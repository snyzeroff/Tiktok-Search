"""Tests de robustesse : chaque test correspond à un défaut corrigé."""
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from tiktok_search import analyze, importer, pipeline
from tiktok_search.config import Settings
from tiktok_search.db import Database
from tiktok_search.enrich import VideoMeta, parse_vtt_segments
from tiktok_search.search import build_match_query, search
from tiktok_search.tiktok_page import (Blocked, PageItem, VideoUnavailable, extract_item_struct,
                                       parse_item_struct)


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path)
    for d in (s.media_dir, s.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    s.transcribe, s.ocr, s.keep_video, s.request_delay, s.llm_provider = "subtitles", False, False, 0, "none"
    return s


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


# --- VTT -----------------------------------------------------------------------

def test_vtt_edge_cases():
    vtt = ("﻿WEBVTT\n\nNOTE commentaire\nsur deux lignes\n\nintro\n00:00.000 --> 00:01.000 align:start\n"
           "Tom &amp; Jerry\n\n2\n00:01.000 --> 00:02.000\n2024\n\n00:02.000 --> 00:03.000\nNOTE importante\n")
    assert [s["text"] for s in parse_vtt_segments(vtt)] == ["Tom & Jerry", "2024", "NOTE importante"]
    assert parse_vtt_segments("") == [] and parse_vtt_segments("n'importe quoi") == []


# --- recherche -------------------------------------------------------------------

@pytest.mark.parametrize("q", ["hello -", "hello .", "a & b", "recette 🍝", "hello —"])
def test_punctuation_tokens_ignored(q):
    assert '""' not in build_match_query(q) and '"-"' not in build_match_query(q)


def test_search_survives_weird_queries(db):
    db.add_favorite("1", "u")
    db.save_video("1", description="recette pâtes", status="done")
    assert [r.video["id"] for r in search(db, "recette 🍝")] == ["1"]
    for q in ['"', "(", "NEAR(", "AND OR NOT", "*", "-", "a" * 600, '"unterminated', "{x}:y", "🍝"]:
        search(db, q)  # ne doit jamais lever d'exception


def test_fts_rowid_migration_and_delete(db):
    for i in range(5):
        db.add_favorite(str(i), "u")
        db.save_video(str(i), description=f"vidéo {i} commune", status="done")
    db.save_video("3", description="remplacée")
    assert sorted(r.video["id"] for r in search(db, "commune")) == ["0", "1", "2", "4"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM videos_fts").fetchone()[0] == 5


# --- import ---------------------------------------------------------------------

def test_txt_export_in_zip():
    txt = "Date: 2024-01-01 10:00:00\nLink: https://www.tiktokv.com/share/video/7300000000000000001/\n\n" \
          "Date: 2024-01-02 10:00:00\nLink: https://www.tiktokv.com/share/video/7300000000000000002/\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("TikTok/Activity/Favorite Videos.txt", txt)
        zf.writestr("TikTok/Activity/Browsing History.txt", "Link: https://www.tiktokv.com/share/video/7399999999999999999/")
    items = importer.parse_export(importer.load_export_bytes(buf.getvalue()))
    assert sorted(i.video_id for i in items) == ["7300000000000000001", "7300000000000000002"]
    assert {i.date for i in items} == {"2024-01-01 10:00:00", "2024-01-02 10:00:00"}


def test_bad_files_give_clean_errors():
    for raw in (b"PK\x03\x04garbage", b"{not json", b"\x00\x01\x02"):
        try:
            data = importer.load_export_bytes(raw)
        except ValueError:
            continue
        assert importer.parse_export(data) == []


def test_zip_bomb_rejected(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("user_data.json", "{" + " " * 2_000_000 + "}")
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED", 1_000_000)
    with pytest.raises(ValueError):
        importer.load_export_bytes(buf.getvalue())


def test_canonical_url_escapes_author():
    url = importer.canonical_url("1", "');alert(1)//")
    assert "'" not in url and "(" not in url


# --- page TikTok -------------------------------------------------------------------

def _page(detail: dict) -> str:
    return ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps({"__DEFAULT_SCOPE__": {"webapp.video-detail": detail}}) + "</script>")


def test_deleted_video_detected():
    with pytest.raises(VideoUnavailable):
        extract_item_struct(_page({"statusCode": 10204, "statusMsg": "item doesn't exist"}), "1")
    assert extract_item_struct("<html>captcha</html>", "1") is None
    assert extract_item_struct('<script id="SIGI_STATE">{broken</script>', "1") is None


def test_parse_item_struct_tolerates_garbage():
    p = parse_item_struct({"author": None, "video": "x", "imagePost": {"images": [None, {"imageURL": "y"}]},
                           "textExtra": [None, {"hashtagName": 5}], "desc": None}, "1")
    assert p.kind == "video" and p.image_urls == [] and p.hashtags == ["5"]


# --- indexation ------------------------------------------------------------------

class FakeClient:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0

    def fetch(self, vid):
        self.calls += 1
        b = self.behaviour(vid, self.calls)
        if isinstance(b, Exception):
            raise b
        return b

    def get_text(self, url):
        return ""

    def download(self, url, dest):
        Path(dest).write_bytes(b"x")

    def close(self):
        pass


def test_unavailable_is_permanent_and_not_retried(db, settings):
    db.add_favorite("1", "u")
    pipeline.process_all(db, settings, client_factory=lambda: FakeClient(lambda v, n: VideoUnavailable("supprimée")))
    assert db.get_video("1")["status"] == "unavailable"
    assert db.videos_to_process() == []


def test_blocked_pauses_then_stops_without_losing_videos(db, settings, monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    for i in range(3):
        db.add_favorite(str(i), "u")
    job = pipeline.Job()
    pipeline.process_all(db, settings, job=job, max_block_pauses=2,
                         client_factory=lambda: FakeClient(lambda v, n: Blocked("429")))
    assert "bloque" in job.message and not job.running
    assert {v["status"] for v in (db.get_video(str(i)) for i in range(3))} == {"pending"}


def test_blocked_then_recovers(db, settings, monkeypatch):
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    db.add_favorite("1", "u")
    ok = PageItem("1", "video", "a", "légende", [], "", 10.0)
    pipeline.process_all(db, settings, client_factory=lambda: FakeClient(lambda v, n: Blocked("x") if n == 1 else ok))
    assert db.get_video("1")["status"] == "done"


def test_degraded_fallback_never_erases(db, settings, monkeypatch):
    db.add_favorite("1", "u", kind="photo")
    db.save_video("1", kind="photo", description="ancienne légende", images=["/media/1/page_01.jpg"],
                  status="done", segments=[{"kind": "ocr", "page": 1, "text": "texte page"}])
    monkeypatch.setattr(pipeline, "fetch_meta", lambda c, v: VideoMeta(v, source="oembed", author="a"))
    pipeline.process_video(db, settings, db.get_video("1"), FakeClient(lambda v, n: None))
    v = db.get_video("1")
    assert v["kind"] == "photo" and v["description"] == "ancienne légende" and v["images"]
    assert db.get_segments("1") and v["author"] == "a"


def test_whisper_failure_keeps_metadata(db, settings, monkeypatch):
    settings.transcribe = "auto"
    db.add_favorite("1", "u")
    monkeypatch.setattr(pipeline.transcribe, "is_available", lambda: True)
    monkeypatch.setattr(pipeline, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(pipeline.enrich, "download_video", lambda v, w: (_ for _ in ()).throw(RuntimeError("yt-dlp HS")))
    page = PageItem("1", "video", "a", "ma légende", [], "", 10.0)
    assert pipeline.process_video(db, settings, db.get_video("1"), FakeClient(lambda v, n: page)) == "done"
    v = db.get_video("1")
    assert v["description"] == "ma légende" and "yt-dlp HS" in v["error"]


def test_imports_during_processing_are_picked_up(db, settings):
    db.add_favorite("1", "u")

    def behaviour(vid, n):
        if vid == "1":
            db.add_favorite("2", "u")  # import pendant l'indexation
        return PageItem(vid, "video", "a", "d", [], "", 1.0)

    pipeline.process_all(db, settings, client_factory=lambda: FakeClient(behaviour))
    assert db.get_video("2")["status"] == "done"


# --- analyse --------------------------------------------------------------------

def test_normalize_ai_garbage():
    out = analyze.normalize_ai({"tools": ["Notion", {"name": "notion"}, 5, {"x": 1}, {"name": "Figma", "url": "javascript:alert(1)"}],
                                "summary": ["pas", "une chaîne"], "steps": "pas une liste"})
    assert [t["name"] for t in out["tools"]] == ["Notion", "Figma"]
    assert out["tools"][1]["url"] == "" and out["steps"] == [] and isinstance(out["summary"], str)
    assert all(out[c] == [] for c in ("websites", "products", "people", "promo_codes"))


def test_parse_json_loose():
    assert analyze.parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert analyze.parse_json_loose('Voici : {"a": 1} fin') == {"a": 1}
    with pytest.raises(analyze.LLMError):
        analyze.parse_json_loose("[1, 2]")


def _fake_gemini(monkeypatch, payloads):
    calls = []

    def post(url, **kw):
        calls.append(kw)
        status, body = payloads[min(len(calls), len(payloads)) - 1]
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(analyze.httpx, "post", post)
    monkeypatch.setattr(analyze.time, "sleep", lambda s: None)
    return calls


def _ok(ai: dict) -> tuple[int, dict]:
    return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(ai)}]}}], "usageMetadata": {}}


def test_gemini_429_then_ok_and_bad_output_not_cached(db, settings, monkeypatch):
    settings.llm_provider, settings.gemini_api_key = "gemini", "k"
    db.add_favorite("1", "u")
    db.save_video("1", description="Notion " * 40, status="done")
    calls = _fake_gemini(monkeypatch, [(429, {"error": {"details": [{"retryDelay": "2s"}]}}),
                                       _ok({"tools": ["Notion"], "summary": "s"})])
    r = analyze.analyze_video(db, settings, "1")
    assert len(calls) == 2 and r["ai"]["tools"][0]["name"] == "Notion"

    calls = _fake_gemini(monkeypatch, [(200, {"candidates": []})])
    r = analyze.analyze_video(db, settings, "1", force=True)
    assert r["error"] and db.get_analysis("1", "text")["result"]["ai"]["tools"]  # l'ancienne reste


def test_cached_only_never_calls_llm(db, settings, monkeypatch):
    settings.llm_provider, settings.gemini_api_key = "gemini", "k"
    db.add_favorite("1", "u")
    db.save_video("1", description="x", status="done")
    calls = _fake_gemini(monkeypatch, [_ok({})])
    r = analyze.analyze_many(db, settings, ["1"], "text", cached_only=True)
    assert calls == [] and r["results"][0]["ai"] is None


def test_csv_formula_injection():
    agg = {"tools": [{"name": "=HYPERLINK(\"x\")", "sources": [{"author": "@a", "where": "", "url": "u"}]}], "count": 1}
    rows = analyze.to_csv(agg).splitlines()
    assert "'=HYPERLINK" in rows[1] and ",'@a," in rows[1]


# --- API web --------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_DATA_DIR", str(tmp_path / "web"))
    import importlib

    from fastapi.testclient import TestClient

    import tiktok_search.web as web
    web = importlib.reload(web)
    return TestClient(web.app, base_url="http://localhost"), web


def test_csrf_and_host_protection(client):
    c, _ = client
    assert c.post("/api/process").status_code == 403                       # pas d'en-tête X-TTS
    assert c.post("/api/process", headers={"X-TTS": "1"}).status_code == 200
    assert c.get("/api/status", headers={"host": "evil.example"}).status_code == 400
    assert c.get("/").headers["x-frame-options"] == "DENY"


def test_api_validation(client):
    c, _ = client
    h = {"X-TTS": "1"}
    assert c.get("/api/search", params={"q": "x", "limit": -1}).status_code == 422
    assert c.get("/api/search", params={"q": "x" * 501}).status_code == 422
    assert c.post("/api/analyze", json={"ids": ["nope"]}, headers=h).status_code == 404
    assert c.post("/api/analyze", json={"ids": ["1"], "mode": "evil"}, headers=h).status_code == 400
    assert c.get("/api/analyze/export", params={"ids": "nope"}).status_code == 404
    assert c.get("/api/analyze/export", params={"ids": "1", "mode": "x"}).status_code == 400
    assert c.get("/api/video/nope").status_code == 404
    assert c.post("/api/import", files={"file": ("x.json", b"{bad")}, headers=h).status_code == 400


def test_auth_token(client, monkeypatch):
    c, web = client
    monkeypatch.setattr(web.settings, "auth_token", "secret")
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status?token=secret").status_code == 200
    assert c.get("/api/status").status_code == 200  # cookie posé


def test_netstat_parsing_any_windows_language():
    from tiktok_search.cli import parse_netstat_pid

    out = """
Connexions actives

  Proto  Adresse locale         Adresse distante       État
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1104
  TCP    127.0.0.1:8000         127.0.0.1:52011        ESTABLISHED     4242
  TCP    127.0.0.1:8000         0.0.0.0:0              ABHÖREN         36876
  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       7
"""
    assert parse_netstat_pid(out, 8000) == 36876
    assert parse_netstat_pid(out, 9000) is None
