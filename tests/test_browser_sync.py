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
    it = {"id": vid, "desc": f"Favori numéro {i} #astuce", "author": {"uniqueId": f"auteur{i}"},
          "textExtra": [{"hashtagName": "astuce"}], "video": {"duration": 30 + i}}
    if i % 5 == 0:
        it["imagePost"] = {"images": [{}, {}]}
    return it


def test_parse_item_list():
    favs, more = browser_sync.parse_item_list({"itemList": [_item(5), {"id": "pas-un-id"}, "x"], "hasMore": True})
    assert more is True and len(favs) == 1
    f = favs[0]
    assert f.kind == "photo" and f.author == "auteur5" and f.hashtags == "#astuce" and f.duration == 35
    assert f.item.url.endswith("/photo/7400000000000000005")
    assert browser_sync.parse_item_list({}) == ([], False)


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
  const r = await fetch('/api/user/collect/item_list/?count=12&cursor=' + cursor);
  const j = await r.json(); cursor = j.cursor; more = j.hasMore; loading = false;
  for (const it of j.itemList) { const d = document.createElement('div'); d.style.height = '400px'; d.textContent = it.id; grid.appendChild(d); }
}
fav.onclick = load;
addEventListener('scroll', () => { if (innerHeight + scrollY >= document.body.scrollHeight - 50) load(); });
</script></body></html>"""


class FakeTikTok(BaseHTTPRequestHandler):
    requests: list[str] = []

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
            items = [_item(i) for i in range(cur, min(cur + PAGE_SIZE, TOTAL))]
            self._send(json.dumps({"itemList": items, "cursor": cur + len(items), "hasMore": cur + len(items) < TOTAL}),
                       "application/json")
        else:
            self.send_error(404)


@pytest.fixture
def fake_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTikTok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeTikTok.requests = []
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


@needs_browser
def test_login_then_incremental_sync(fake_site, tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_BROWSER_PROFILE", str(tmp_path / "profile"))
    db = Database(tmp_path / "t.db")
    settings = SimpleNamespace(browser="chromium", browser_headless=True, tiktok_username="", tmp_dir=tmp_path)
    # La connexion ouvre normalement une fenêtre visible : ici on force le mode sans fenêtre.
    monkeypatch.setattr(browser_sync, "_launch", _headless(browser_sync._launch))

    msgs: list[str] = []
    r = browser_sync.sync_into_db(db, settings, login=True, base=fake_site, progress=msgs.append)
    assert r == {"username": "moi", "found": TOTAL, "added": TOTAL, "complete": True}
    assert db.stats()["total"] == TOTAL and db.kv_get(browser_sync.KV_FULL_DONE) == "1"
    assert any("favoris lus" in m for m in msgs)

    # Réouverture : la session est gardée dans le profil, et on s'arrête dès la 1re page déjà connue.
    FakeTikTok.requests = []
    r = browser_sync.sync_into_db(db, settings, base=fake_site)
    assert r["added"] == 0 and r["complete"] is False
    assert FakeTikTok.requests.count("/api/user/collect/item_list/") == 1
    assert "/login" not in FakeTikTok.requests


@needs_browser
def test_sync_without_session_asks_to_login(fake_site, tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_BROWSER_PROFILE", str(tmp_path / "vide"))
    db = Database(tmp_path / "t.db")
    settings = SimpleNamespace(browser="chromium", browser_headless=True, tiktok_username="", tmp_dir=tmp_path)
    with pytest.raises(browser_sync.NotLoggedIn):
        browser_sync.sync_into_db(db, settings, base=fake_site)


def _headless(launch):
    def wrapped(pw, profile, headless, channel):
        return launch(pw, profile, True, channel)
    return wrapped
