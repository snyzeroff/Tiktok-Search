"""Indexation rapide : deux passes, téléchargement direct, Whisper sur le MP4, limiteur partagé."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tiktok_search import pipeline
from tiktok_search.config import Settings
from tiktok_search.db import Database
from tiktok_search.search import search
from tiktok_search.tiktok_page import PageItem, parse_item_struct


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.media_dir.mkdir(parents=True)
    s.tmp_dir.mkdir(parents=True)
    s.transcribe, s.ocr, s.keep_video, s.request_delay, s.workers = "auto", False, False, 0, 2
    return s


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


class Client:
    """Faux TikTok : v-sub a des sous-titres, les autres non (→ Whisper)."""

    def __init__(self, on_fetch=None):
        self.downloads: list[str] = []
        self.on_fetch = on_fetch

    def fetch(self, vid):
        if self.on_fetch:
            self.on_fetch(vid)
        subs = [{"lang": "fra-FR", "url": "https://s/fr", "format": "webvtt"}] if vid == "v-sub" else []
        return PageItem(vid, "video", "chef", f"légende {vid}", [], "", 20.0, [], subs, play_url=f"https://cdn/{vid}.mp4")

    def get_text(self, url):
        return "WEBVTT\n\n00:01.000 --> 00:02.000\nsous-titre tiktok\n"

    def download(self, url, dest, max_bytes=0, expect=""):
        self.downloads.append(url)
        Path(dest).write_bytes(b"mp4")

    def close(self):
        pass


@pytest.fixture
def whisper(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(pipeline.transcribe, "is_available", lambda: True)

    def fake(path, model, lang="", threads=0):
        calls.append(Path(path).name)
        return [{"start": 3.0, "end": 4.0, "text": "transcription whisper"}]

    monkeypatch.setattr(pipeline.transcribe, "transcribe_segments", fake)
    monkeypatch.setattr(pipeline.enrich, "download_video",
                        lambda v, w: (_ for _ in ()).throw(AssertionError("yt-dlp ne doit pas être appelé")))
    return calls


def test_first_pass_makes_videos_searchable_before_whisper(db, settings, whisper):
    db.add_favorite("v1", "u")
    client = Client()
    assert pipeline.process_video(db, settings, db.get_video("v1"), client, defer_media=True, play_urls={}) == "done"
    v = db.get_video("v1")
    assert v["status"] == "done" and v["media_pending"] == 1 and not whisper
    assert [r.video["id"] for r in search(db, "légende")] == ["v1"]
    assert db.stats()["media_pending"] == 1


def test_two_passes_end_to_end(db, settings, whisper):
    for vid in ("v1", "v2", "v-sub"):
        db.add_favorite(vid, "u")
    client = Client()
    job = pipeline.Job()
    assert pipeline.process_all(db, settings, job=job, client_factory=lambda: client) == 3
    # Whisper seulement pour les vidéos sans sous-titres, directement sur le MP4, lien direct de la page.
    assert sorted(whisper) == ["v1.mp4", "v2.mp4"]
    assert sorted(client.downloads) == ["https://cdn/v1.mp4", "https://cdn/v2.mp4"]
    for vid in ("v1", "v2"):
        v = db.get_video(vid)
        assert v["media_pending"] == 0 and v["transcript"] == "transcription whisper"
    assert db.get_video("v-sub")["transcript"] == "sous-titre tiktok"
    assert search(db, "whisper")[0].hits[0]["start"] == 3.0
    assert db.stats()["media_pending"] == 0 and not job.running and job.phase == ""
    assert not list(settings.tmp_dir.iterdir())  # fichiers temporaires nettoyés


def test_new_favorites_jump_ahead_of_transcriptions(db, settings, whisper, monkeypatch):
    monkeypatch.setattr(pipeline, "MEDIA_CHUNK", 1)
    settings.workers = 1
    for vid in ("a", "b", "c"):
        db.add_favorite(vid, "u")
    order: list[str] = []
    real = pipeline.transcribe.transcribe_segments

    def tracking(path, *a):
        order.append("whisper:" + Path(path).stem)
        if Path(path).stem == "a":
            db.add_favorite("new", "u")  # arrive pendant la 2e passe
        return real(path, *a)

    monkeypatch.setattr(pipeline.transcribe, "transcribe_segments", tracking)
    client = Client(on_fetch=lambda vid: order.append("fetch:" + vid))
    pipeline.process_all(db, settings, client_factory=lambda: client)
    # Les 3 légendes d'abord, puis : « new » est indexée dès la fin de la transcription en cours.
    assert order[:4] == ["fetch:a", "fetch:b", "fetch:c", "whisper:a"]
    assert order[4] == "fetch:new"
    assert sorted(order[5:]) == ["whisper:b", "whisper:c", "whisper:new"]
    assert db.get_video("new")["transcript"] == "transcription whisper"


def test_direct_link_failure_falls_back_to_ytdlp(db, settings, whisper, monkeypatch):
    db.add_favorite("v1", "u")

    class Expired(Client):
        def download(self, url, dest, max_bytes=0, expect=""):
            raise ValueError("lien expiré")

    def ytdlp(vid, work):
        work.mkdir(parents=True, exist_ok=True)
        (work / "y.mp4").write_bytes(b"mp4")
        return work / "y.mp4"

    monkeypatch.setattr(pipeline.enrich, "download_video", ytdlp)
    pipeline.process_all(db, settings, client_factory=Expired)
    assert whisper == ["y.mp4"] and db.get_video("v1")["transcript"]


def test_media_failure_keeps_video_searchable(db, settings, whisper, monkeypatch):
    db.add_favorite("v1", "u")
    monkeypatch.setattr(pipeline, "_media_work", lambda *a: (_ for _ in ()).throw(RuntimeError("disque plein")))
    pipeline.process_all(db, settings, client_factory=Client)
    v = db.get_video("v1")
    assert v["status"] == "done" and v["media_pending"] == 0 and "disque plein" in v["error"]
    assert search(db, "légende")


def test_reindex_clears_media_pending(db, settings, whisper):
    db.add_favorite("v1", "u")
    pipeline.process_video(db, settings, db.get_video("v1"), Client(), defer_media=True)
    db.reset_all()
    assert db.get_video("v1")["media_pending"] == 0 and db.stats()["media_pending"] == 0


def test_throttle_spaces_requests_across_threads():
    import threading

    t = pipeline.Throttle(0.05)
    stamps: list[float] = []
    threads = [threading.Thread(target=lambda: (t.wait(), stamps.append(time.monotonic()))) for _ in range(4)]
    start = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert time.monotonic() - start >= 0.14  # 4 requêtes → au moins 3 intervalles
    pipeline.Throttle(0).wait()  # 0 = pas de limite, ne bloque pas


def test_play_url_parsed_from_page():
    item = {"id": "1", "video": {"playAddr": "https://v16.tiktokcdn.com/x.mp4", "duration": 12}}
    assert parse_item_struct(item, "1").play_url == "https://v16.tiktokcdn.com/x.mp4"
    item = {"id": "1", "video": {"playAddr": "", "bitrateInfo": [{"PlayAddr": {"UrlList": ["https://b/1.mp4"]}}]}}
    assert parse_item_struct(item, "1").play_url == "https://b/1.mp4"
    photo = {"id": "1", "video": {"playAddr": "https://x"}, "imagePost": {"images": [{"imageURL": {"urlList": ["https://i/1"]}}]}}
    assert parse_item_struct(photo, "1").play_url == ""
