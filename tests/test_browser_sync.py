"""Synchro des favoris par le navigateur.

Le test de bout en bout pilote un vrai Chromium contre un faux site TikTok local qui imite ce
que fait le vrai : cookie ``sessionid`` après connexion, ``/passport/web/account/info/``, onglet
« Favoris » qui charge ``/api/user/collect/item_list/`` page par page pendant le défilement.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tiktok_search import browser_sync
from tiktok_search.db import Database

PAGE_SIZE = 12
TOTAL = 30


def _item(i: int) -> dict:
    vid = str(7400000000000000000 + i)
    it = {"id": vid, "desc": f"Favori numéro {i} #astuce", "author": {"uniqueId": f"auteur{i}", "signature": "x" * 500},
          "textExtra": [{"hashtagName": "astuce", "start": 0}], "stats": {"playCount": 1},
          "video": {"duration": 30 + i, "cover": f"https://cdn/c{i}.jpg", "playAddr": f"https://cdn/v{i}.mp4",
                    "subtitleInfos": [], "bitrateInfo": [{"PlayAddr": {"UrlList": ["https://cdn/b.mp4"]}}] * 5}}
    if i % 5 == 0:
        it["imagePost"] = {"images": [{"imageURL": {"urlList": [f"https://cdn/p{i}-1.jpg", "https://alt"]}}]}
    return it


def test_parse_item_list():
    favs, more = browser_sync.parse_item_list({"itemList": [_item(5), {"id": "pas-un-id"}, "x"], "hasMore": True})
    assert more is True and len(favs) == 1
    f = favs[0]
    assert f.kind == "photo" and f.author == "auteur5" and f.hashtags == "#astuce" and f.duration == 35
    assert f.item.url.endswith("/photo/7400000000000000005")
    assert browser_sync.parse_item_list({}) == ([], False)
    raw = browser_sync.parse_item_list({"itemList": [_item(1)]})[0][0].raw
    # Allégé : rien d'inutile (stats, bio de l'auteur, toutes les qualités vidéo), mais tout ce que l'indexation lit.
    assert "stats" not in raw and raw["author"] == {"uniqueId": "auteur1"} and "bitrateInfo" not in raw["video"]
    assert raw["video"]["subtitleInfos"] == [] and raw["video"]["playAddr"] == "https://cdn/v1.mp4"


def test_store_keeps_tiktok_order_and_prefills(tmp_path):
    db = Database(tmp_path / "t.db")
    favs, _ = browser_sync.parse_item_list({"itemList": [_item(1), _item(2)]})
    r = browser_sync.SyncResult("moi", favs, complete=True)
    assert browser_sync.store(db, r, now=1_700_000_000) == 2
    assert browser_sync.store(db, r, now=1_700_000_100) == 0  # déjà connus
    v1, v2 = db.get_video(favs[0].item.video_id), db.get_video(favs[1].item.video_id)
    assert v1["favorited_at"] > v2["favorited_at"]  # le plus récent en premier
    assert v1["description"].startswith("Favori numéro 1") and v1["status"] == "pending"
    assert db.kv_get(browser_sync.KV_USERNAME) == "moi" and db.kv_get(browser_sync.KV_FULL_DONE) == "1"


# --- faux TikTok -------------------------------------------------------------------

PROFILE_HTML = """<!doctype html><html><body style="margin:0">
<div role="tablist"><p role="tab">Vidéos</p><p role="tab" data-e2e="favorites-tab" id="fav">Favoris</p></div>
<div id="grid"></div>
<script>
let cursor = 0, more = true, loading = false;
async function load() {
  if (!more || loading) return; loading = true;
  const r = await fetch('/api/user/collect/item_list/?count=12&cursor=' + cursor, {headers: {'x-ui': '1'}});
  const j = await r.json(); cursor = j.cursor; more = j.hasMore; loading = false;
  for (const it of j.itemList) { const d = document.createElement('div'); d.style.height = '400px'; d.textContent = it.id; grid.appendChild(d); }
}
fav.onclick = load;
addEventListener('scroll', () => { if (innerHeight + scrollY >= document.body.scrollHeight - 50) load(); });
</script></body></html>"""


class FakeTikTok(BaseHTTPRequestHandler):
    requests: list[str] = []
    cursors: list[tuple[int, bool]] = []   # (cursor, demandé par la page elle-même ?)
    reject_direct = False                 # refuse les requêtes qui ne viennent pas de l'interface
    broken_from: int | None = None        # simule une coupure à partir de ce curseur

    def log_message(self, *a):  # silence
        pass

    def _send(self, body: str, ctype: str = "text/html", headers: dict | None = None) -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        logged = "sessionid=" in (self.headers.get("Cookie") or "")
        FakeTikTok.requests.append(url.path)
        if url.path == "/login":
            # Simule la connexion de l'utilisateur dans la fenêtre.
            self._send("<script>setTimeout(() => { document.cookie = 'sessionid=abc; path=/; max-age=31536000'; location = '/'; }, 300)</script>")
        elif url.path == "/":
            self._send('<a data-e2e="nav-profile" href="/@moi">Profil</a>')
        elif url.path == "/passport/web/account/info/":
            self._send(json.dumps({"data": {"username": "moi"} if logged else {}}), "application/json")
        elif url.path == "/@moi":
            self._send(PROFILE_HTML)
        elif url.path == "/api/user/collect/item_list/":
            cur = int(parse_qs(url.query).get("cursor", ["0"])[0])
            ui = self.headers.get("x-ui") == "1"
            FakeTikTok.cursors.append((cur, ui))
            if (FakeTikTok.reject_direct and not ui) or (FakeTikTok.broken_from is not None and cur >= FakeTikTok.broken_from):
                self._send(json.dumps({"statusCode": 10201, "itemList": None}), "application/json")
                return
            items = [_item(i) for i in range(cur, min(cur + PAGE_SIZE, TOTAL))]
            self._send(json.dumps({"itemList": items, "cursor": cur + len(items), "hasMore": cur + len(items) < TOTAL}),
                       "application/json")
        else:
            self.send_error(404)


@pytest.fixture
def fake_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTikTok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeTikTok.requests, FakeTikTok.cursors = [], []
    FakeTikTok.reject_direct, FakeTikTok.broken_from = False, None
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _chromium_ok() -> bool:
    if not browser_sync.is_available():
        return False
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_browser = pytest.mark.skipif(not _chromium_ok(), reason="Chromium de Playwright indisponible")


FAST = (0.01, 0.02)  # pause entre deux pages (réelle : 0,4 à 1 s)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_BROWSER_PROFILE", str(tmp_path / "profile"))
    # La connexion ouvre normalement une fenêtre visible : ici on force le mode sans fenêtre.
    monkeypatch.setattr(browser_sync, "_launch", _headless(browser_sync._launch))
    db = Database(tmp_path / "t.db")
    settings = SimpleNamespace(browser="chromium", browser_headless=True, tiktok_username="", tmp_dir=tmp_path)
    return db, settings


@needs_browser
def test_login_then_incremental_sync(fake_site, tmp_path, monkeypatch):
    db, settings = _setup(tmp_path, monkeypatch)
    msgs: list[str] = []
    r = browser_sync.sync_into_db(db, settings, login=True, base=fake_site, progress=msgs.append, pace=FAST)
    assert r == {"username": "moi", "found": TOTAL, "added": TOTAL, "complete": True, "mode": "api"}
    # 1re page chargée par l'interface, les suivantes en requêtes directes (sans défilement).
    assert FakeTikTok.cursors == [(0, True), (12, False), (24, False)]
    assert db.stats()["total"] == TOTAL and db.kv_get(browser_sync.KV_FULL_DONE) == "1"
    assert db.cache_count() == TOTAL and db.kv_get(browser_sync.KV_RESUME) is None
    assert any("favoris lus" in m for m in msgs)

    # Réouverture : la session est gardée dans le profil, et on s'arrête dès la 1re page déjà connue.
    FakeTikTok.requests, FakeTikTok.cursors = [], []
    r = browser_sync.sync_into_db(db, settings, base=fake_site, pace=FAST)
    assert r["added"] == 0 and r["complete"] is False
    assert FakeTikTok.cursors == [(0, True)]
    assert "/login" not in FakeTikTok.requests


@needs_browser
def test_direct_requests_refused_falls_back_to_scrolling(fake_site, tmp_path, monkeypatch):
    db, settings = _setup(tmp_path, monkeypatch)
    FakeTikTok.reject_direct = True
    r = browser_sync.sync_into_db(db, settings, login=True, base=fake_site, pace=FAST)
    assert r["added"] == TOTAL and r["complete"] and r["mode"] == "scroll"


@needs_browser
def test_interrupted_sync_keeps_progress_and_resumes(fake_site, tmp_path, monkeypatch):
    db, settings = _setup(tmp_path, monkeypatch)
    FakeTikTok.broken_from = 24  # coupure au milieu de la 1re synchro complète
    r = browser_sync.sync_into_db(db, settings, login=True, base=fake_site, pace=FAST)
    assert r["complete"] is False and r["added"] == 24
    assert db.stats()["total"] == 24                       # déjà enregistrés malgré la coupure
    assert db.kv_get(browser_sync.KV_RESUME) == "24" and db.kv_get(browser_sync.KV_FULL_DONE) is None

    FakeTikTok.broken_from, FakeTikTok.cursors = None, []
    r = browser_sync.sync_into_db(db, settings, base=fake_site, pace=FAST)
    assert FakeTikTok.cursors == [(0, True), (24, False)]  # reprend directement à 24
    assert r["complete"] and db.stats()["total"] == TOTAL and db.kv_get(browser_sync.KV_FULL_DONE) == "1"


@needs_browser
def test_sync_without_session_asks_to_login(fake_site, tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_BROWSER_PROFILE", str(tmp_path / "vide"))
    db = Database(tmp_path / "t.db")
    settings = SimpleNamespace(browser="chromium", browser_headless=True, tiktok_username="", tmp_dir=tmp_path)
    with pytest.raises(browser_sync.NotLoggedIn):
        browser_sync.sync_into_db(db, settings, base=fake_site, pace=FAST)


def _headless(launch):
    def wrapped(pw, profile, headless, channel):
        return launch(pw, profile, True, channel)
    return wrapped
