import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tiktok_search import analyze, importer, screens
from tiktok_search.config import Settings
from tiktok_search.db import Database
from tiktok_search.enrich import parse_vtt_segments, pick_subtitle
from tiktok_search.search import search, segment_hits
from tiktok_search.tiktok_page import extract_item_struct, parse_item_struct


def _page(item: dict) -> str:
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return ('<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(data) + "</script></html>")


def test_parse_carousel_page():
    item = {
        "id": "7400000000000000001", "desc": "5 outils IA gratuits #ia #productivite",
        "author": {"uniqueId": "techfr"},
        "imagePost": {"title": "Outils IA", "images": [
            {"imageURL": {"urlList": ["https://cdn/1.jpg", "https://cdn/1b.jpg"]}},
            {"imageURL": {"urlList": ["https://cdn/2.jpg"]}}]},
        "video": {"cover": "https://cdn/cover.jpg", "duration": 0},
        "textExtra": [{"hashtagName": "ia"}, {"hashtagName": "productivite"}],
    }
    p = parse_item_struct(extract_item_struct(_page(item), item["id"]), item["id"])
    assert p.kind == "photo"
    assert p.image_urls == ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    assert p.author == "techfr" and p.description.startswith("Outils IA")
    assert p.hashtags == ["ia", "productivite"] and p.duration is None


def test_parse_video_page_with_subtitles():
    item = {"id": "7", "desc": "Recette", "author": {"uniqueId": "chef"},
            "video": {"duration": 42, "cover": "c", "subtitleInfos": [
                {"LanguageCodeName": "eng-US", "Url": "https://s/en", "Format": "webvtt"},
                {"LanguageCodeName": "fra-FR", "Url": "https://s/fr", "Format": "webvtt"}]}}
    p = parse_item_struct(extract_item_struct(_page(item), "7"), "7")
    assert p.kind == "video" and p.duration == 42.0
    assert pick_subtitle(p.subtitles)["url"] == "https://s/fr"


def test_vtt_segments_timestamps():
    vtt = ("WEBVTT\n\n00:00:01.500 --> 00:00:03.000\nBonjour\n\n00:03.000 --> 00:04.000\nBonjour\n\n"
           "01:02.250 --> 01:05.000\n<c>la suite</c>\n")
    assert parse_vtt_segments(vtt) == [
        {"start": 1.5, "end": 4.0, "text": "Bonjour"},
        {"start": 62.25, "end": 65.0, "text": "la suite"},
    ]


def test_photo_links():
    items = importer.parse_links_text("https://www.tiktok.com/@a/photo/7400000000000000002")
    assert items[0].video_id == "7400000000000000002"
    assert importer.kind_from_url(items[0].url) == "photo"
    assert importer.canonical_url("1", "a", "photo") == "https://www.tiktok.com/@a/photo/1"


def _frame(path: Path, text: str, color: str) -> Path:
    im = Image.new("RGB", (180, 320), color)
    d = ImageDraw.Draw(im)
    for i in range(0, 320, 40):
        d.rectangle([10, i, 10 + (hash(text + str(i)) % 150), i + 20], fill="white")
    im.save(path)
    return path


def test_smart_screens_dedupe(tmp_path):
    a = _frame(tmp_path / "a.jpg", "A", "black")
    b = _frame(tmp_path / "b.jpg", "B", "navy")
    frames = [(a, 0.0), (a, 1.0), (a, 2.0), (b, 3.0), (b, 4.0), (a, 5.0)]
    kept = screens.select_distinct(frames, threshold=5)
    assert [s.t for s in kept] == [0.0, 3.0, 5.0]
    assert [s.t for s in screens.select_distinct(frames, threshold=5, max_screens=2)] == [0.0, 3.0]
    assert screens.to_jpeg_bytes(a)[:2] == b"\xff\xd8"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.add_favorite("v1", "u", kind="video")
    d.save_video("v1", author="dev", description="Mes outils", status="done", kind="video", segments=[
        {"kind": "transcript", "start": 2.0, "end": 5.0, "text": "salut à tous aujourd'hui trois outils"},
        {"kind": "transcript", "start": 42.0, "end": 46.0, "text": "le premier c'est notion point so pour organiser"},
        {"kind": "transcript", "start": 70.0, "end": 74.0, "text": "avec le code PROMO20 sur canva.com"},
        {"kind": "ocr", "start": 43.0, "end": 47.0, "text": "NOTION — gratuit"},
    ], transcript="salut à tous aujourd'hui trois outils le premier c'est notion point so pour organiser avec le code PROMO20 sur canva.com",
        ocr_text="NOTION — gratuit")
    d.add_favorite("p1", "u", kind="photo")
    d.save_video("p1", author="design", description="Palettes de couleurs", status="done", kind="photo",
                 images=["/media/p1/page_01.jpg", "/media/p1/page_02.jpg", "/media/p1/page_03.jpg"],
                 segments=[{"kind": "ocr", "page": 1, "text": "Top 3 sites"},
                           {"kind": "ocr", "page": 3, "text": "coolors.co génère des palettes"}],
                 ocr_text="[p.1] Top 3 sites | [p.3] coolors.co génère des palettes")
    return d


def test_search_returns_timecodes_and_pages(db):
    res = {r.video["id"]: r for r in search(db, "notion")}
    assert set(res) == {"v1"}
    hits = res["v1"].hits
    assert [(h["kind"], h["start"]) for h in hits] == [("transcript", 42.0), ("ocr", 43.0)]
    assert "\x02notion\x03" in hits[0]["text"]

    res = search(db, "palettes")
    assert res[0].video["id"] == "p1" and res[0].video["images"][2].endswith("page_03.jpg")
    assert [h["page"] for h in res[0].hits] == [3]


def test_search_kind_filter_and_cross_segment_fallback(db):
    assert search(db, "palettes", kind="video") == []
    # "trois" et "notion" sont dans deux phrases différentes → repli sur OU au niveau des segments
    hits = segment_hits(db, "v1", "trois notion")
    assert {h["start"] for h in hits} >= {2.0, 42.0}


def test_full_transcript_highlight(db):
    segs = segment_hits(db, "v1", "organiser", kinds=["transcript"], all_segments=True)
    assert len(segs) == 3 and [s["match"] for s in segs] == [False, True, False]


def test_local_extract(db):
    loc = analyze.local_extract(db.get_video("v1"), db.get_segments("v1"))
    domains = {d["value"]: d["where"] for d in loc["domains"]}
    assert domains["notion.so"] == ["0:42"] and domains["canva.com"] == ["1:10"]
    assert [c["value"] for c in loc["promo_codes"]] == ["PROMO20"]


def test_analyze_without_llm_and_cache(db, tmp_path):
    s = Settings(data_dir=tmp_path)
    s.llm_provider = "none"
    r = analyze.analyze_video(db, s, "v1")
    assert r["ai"] is None and not r["cached"]
    assert analyze.analyze_video(db, s, "v1")["cached"]
    assert "notion.so" in db.get_video("v1")["entities"]
    assert search(db, "canva", fields=["entities"])[0].video["id"] == "v1"


def test_analyze_gemini_mocked_and_aggregate(db, tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path)
    s.llm_provider, s.gemini_api_key = "gemini", "k"
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            ai = {"summary": "3 outils de productivité", "topic": "productivité",
                  "tools": [{"name": "Notion", "category": "app", "url": "https://notion.so", "details": "notes", "where": "0:42"}],
                  "websites": [], "products": [], "people": [], "promo_codes": [{"name": "PROMO20", "where": "1:10"}],
                  "key_points": ["Notion est gratuit"], "steps": []}
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(ai)}]}}],
                    "usageMetadata": {"promptTokenCount": 400, "candidatesTokenCount": 120}}

    def fake_post(url, **kw):
        calls.append((url, kw))
        return Resp()

    monkeypatch.setattr(analyze.httpx, "post", fake_post)
    data = analyze.analyze_many(db, s, ["v1"], "text")
    assert len(calls) == 1
    body = calls[0][1]["json"]
    assert "notion point so" in body["contents"][0]["parts"][0]["text"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    agg = data["aggregate"]
    assert agg["tools"][0]["name"] == "Notion" and agg["tools"][0]["sources"][0]["where"] == "0:42"
    assert data["results"][0]["usage"]["input_tokens"] == 400
    # deuxième appel : servi par le cache, aucun nouvel appel payant
    analyze.analyze_many(db, s, ["v1"], "text")
    assert len(calls) == 1
    md = analyze.to_markdown(agg)
    assert "Notion" in md and "0:42" in md
    assert "Notion" in analyze.to_csv(agg)


def test_migration_from_v1(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE videos (id TEXT PRIMARY KEY, url TEXT NOT NULL, author TEXT DEFAULT '', description TEXT DEFAULT '',
          hashtags TEXT DEFAULT '', transcript TEXT DEFAULT '', ocr_text TEXT DEFAULT '', thumbnail TEXT DEFAULT '',
          duration REAL, favorited_at TEXT DEFAULT '', source TEXT DEFAULT '', status TEXT DEFAULT 'pending',
          error TEXT DEFAULT '', updated_at REAL);
        CREATE VIRTUAL TABLE videos_fts USING fts5(id UNINDEXED, description, hashtags, transcript, ocr_text, author);
        INSERT INTO videos (id, url, description, status) VALUES ('1', 'u', 'ancienne vidéo cuisine', 'done');
    """)
    conn.commit()
    conn.close()
    d = Database(path)
    assert d.get_video("1")["kind"] == "video"
    assert [r.video["id"] for r in search(d, "cuisine")] == ["1"]


def test_pipeline_carousel_and_video(tmp_path, monkeypatch):
    from tiktok_search import pipeline
    from tiktok_search.tiktok_page import PageItem

    s = Settings(data_dir=tmp_path)
    s.media_dir.mkdir(parents=True)
    s.tmp_dir.mkdir(parents=True)
    s.transcribe, s.ocr, s.keep_video = "subtitles", False, False
    d = Database(s.db_path)
    d.add_favorite("p1", "u", kind="video")  # le type réel est détecté à la lecture de la page
    d.add_favorite("v1", "u")

    class FakeClient:
        def fetch(self, vid):
            if vid == "p1":
                return PageItem("p1", "photo", "design", "Palettes", ["design"], "https://c/cover",
                                None, ["https://c/1", "https://c/2"])
            return PageItem("v1", "video", "chef", "Pâtes", [], "https://c/cover", 30.0, [],
                            [{"lang": "fra-FR", "url": "https://s/fr", "format": "webvtt"}])

        def get_text(self, url):
            return "WEBVTT\n\n00:05.000 --> 00:07.000\nune pincée de sel\n"

        def download(self, url, dest):
            Path(dest).write_bytes(b"img")

    for vid in ("p1", "v1"):
        pipeline.process_video(d, s, d.get_video(vid), FakeClient())
    p = d.get_video("p1")
    assert p["status"] == "done" and p["kind"] == "photo"
    assert p["images"] == ["/media/p1/page_01.jpg", "/media/p1/page_02.jpg"]
    assert p["thumbnail"] == "/media/p1/cover.jpg"
    v = d.get_video("v1")
    assert v["status"] == "done" and v["transcript"] == "une pincée de sel"
    assert search(d, "sel")[0].hits[0]["start"] == 5.0
