"""Synchronisation des favoris en pilotant un vrai navigateur (Playwright).

Principe :
1. ``login`` ouvre une fenêtre de navigateur sur tiktok.com : tu te connectes comme d'habitude
   (QR code avec l'appli, Google, e-mail…). La session est gardée dans un profil de navigateur
   dédié, sur ton ordinateur uniquement.
2. ``sync`` rouvre ce profil (sans fenêtre si possible), va sur ton profil, onglet « Favoris »,
   et lit les réponses JSON que la page TikTok reçoit elle-même pendant qu'on fait défiler
   (``/api/user/collect/item_list/``). Aucune requête « inventée » : on ne fait que regarder
   ce que le site charge pour afficher tes favoris.

Pour des milliers de favoris, une fois la 1re page chargée par la page elle-même, les suivantes
sont demandées directement depuis la page (même session, même signature que le site), sans faire
défiler ni afficher des milliers de miniatures. Si TikTok refuse, on revient au défilement.
Chaque page est enregistrée dès qu'elle arrive, et une 1re synchro interrompue reprend où elle
s'était arrêtée.

La synchro est incrémentale : TikTok renvoie les favoris du plus récent au plus ancien, donc
après une première synchro complète on s'arrête dès qu'une page ne contient plus rien de nouveau.

Les données de chaque favori (légende, sous-titres, miniature, lien vidéo) sont aussi gardées de
côté pour l'indexation, qui n'a alors plus besoin d'ouvrir la page de chaque vidéo.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .importer import FavoriteItem, canonical_url

log = logging.getLogger(__name__)

TIKTOK = "https://www.tiktok.com"
FAVORITES_API = "/api/user/collect/item_list"
COLLECTION_LIST_API = "/api/user/collection_list"
COLLECTION_ITEMS_API = "/api/collection/item_list"
COLLECTIONS_TAB_TEXT = re.compile(r"^\s*(collections?|colecciones|cole[cç][oõ]es|sammlungen|raccolte|kolekcje|collecties)\s*$", re.I)
COLLECTIONS_TAB_SELECTORS = ('[data-e2e="collection-tab"]', '[data-e2e="favorites-collection-tab"]',
                             '[data-e2e="collections-tab"]')
SESSION_COOKIES = ("sessionid", "sessionid_ss")
FAVORITES_TAB_SELECTORS = ('[data-e2e="favorites-tab"]', '[data-e2e="favorite-tab"]', '[data-e2e="collect-tab"]')
# Nom de l'onglet selon la langue de TikTok.
FAVORITES_TAB_TEXT = re.compile(r"^\s*(favoris|favorites?|favoritos|favoriten|preferiti|ulubione|favorieten)\s*$", re.I)
OWN_PROFILE_SELECTORS = ('[data-e2e="edit-profile-entrance"]', '[data-e2e="edit-profile"]')
CAPTCHA_SELECTORS = ("#captcha-verify-container", ".captcha_verify_container", "#captcha_container",
                     '[class*="captcha-verify"]')
KV_USERNAME = "browser_username"
KV_LAST_SYNC = "browser_last_sync"
KV_FULL_DONE = "browser_full_sync_done"
KV_SESSION = "browser_session"
KV_RESUME = "browser_resume_cursor"  # où reprendre une 1re synchro complète interrompue

_lock = threading.Lock()  # un seul navigateur à la fois sur le profil


class SyncError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


class NotLoggedIn(SyncError):
    pass


class Blocked(SyncError):
    """Vérification (captcha) ou page qui ne charge rien : à retenter avec une fenêtre visible."""


@dataclass
class FavoriteMeta:
    """Ce que la liste des favoris dit déjà de chaque publication (utile avant l'indexation)."""
    item: FavoriteItem
    kind: str = "video"
    author: str = ""
    description: str = ""
    hashtags: str = ""
    duration: float | None = None
    raw: dict = field(default_factory=dict, repr=False)  # données utiles à l'indexation (allégées)


@dataclass
class SyncResult:
    username: str
    favorites: list[FavoriteMeta] = field(default_factory=list)
    complete: bool = False  # True si on est allé jusqu'au bout de la liste
    added: int = 0          # nouveaux favoris enregistrés (au fil de l'eau)
    mode: str = ""          # "api" (requêtes directes) ou "scroll" (défilement)


# --- lecture des réponses ---------------------------------------------------------

def slim_item(it: dict) -> dict:
    """Garde seulement ce dont l'indexation a besoin (même forme que la page d'une vidéo)."""
    video = it.get("video") if isinstance(it.get("video"), dict) else {}
    out: dict = {k: it[k] for k in ("id", "desc") if k in it}
    if isinstance(it.get("author"), dict):
        out["author"] = {"uniqueId": it["author"].get("uniqueId", "")}
    v = {k: video[k] for k in ("duration", "cover", "originCover", "playAddr", "downloadAddr", "subtitleInfos")
         if k in video}
    if video.get("bitrateInfo") and not (v.get("playAddr") or v.get("downloadAddr")):
        v["bitrateInfo"] = video["bitrateInfo"][:1]
    out["video"] = v
    if isinstance(it.get("imagePost"), dict):
        ip = it["imagePost"]
        out["imagePost"] = {"title": ip.get("title", ""), "images": [
            {"imageURL": {"urlList": ((img.get("imageURL") or {}).get("urlList") or [])[:1]}}
            for img in ip.get("images") or [] if isinstance(img, dict)]}
    out["textExtra"] = [{"hashtagName": t["hashtagName"]} for t in it.get("textExtra") or []
                        if isinstance(t, dict) and t.get("hashtagName")]
    out["challenges"] = [{"title": c["title"]} for c in it.get("challenges") or [] if isinstance(c, dict) and c.get("title")]
    return out


def parse_item_list(payload: dict) -> tuple[list[FavoriteMeta], bool]:
    """Réponse JSON de ``/api/user/collect/item_list/`` → (favoris, reste-t-il des pages)."""
    out: list[FavoriteMeta] = []
    for it in payload.get("itemList") or []:
        if not isinstance(it, dict):
            continue
        vid = str(it.get("id") or "").strip()
        if not vid.isdigit():
            continue
        author = it.get("author") or {}
        author = (author.get("uniqueId") if isinstance(author, dict) else str(author)) or ""
        kind = "photo" if it.get("imagePost") else "video"
        tags = [t.get("hashtagName") for t in it.get("textExtra") or [] if isinstance(t, dict) and t.get("hashtagName")]
        tags += [c.get("title") for c in it.get("challenges") or [] if isinstance(c, dict) and c.get("title")]
        hashtags = " ".join(dict.fromkeys(f"#{t}" for t in tags))
        dur = (it.get("video") or {}).get("duration")
        out.append(FavoriteMeta(
            item=FavoriteItem(vid, canonical_url(vid, author, kind), "", "favorite"),
            kind=kind, author=author, description=str(it.get("desc") or ""), hashtags=hashtags,
            duration=float(dur) if isinstance(dur, (int, float)) and dur > 0 else None,
            raw=slim_item(it),
        ))
    return out, bool(payload.get("hasMore"))


# --- navigateur ---------------------------------------------------------------------

def default_profile_dir() -> Path:
    """Hors du dossier du projet (souvent dans OneDrive, qui verrouille les fichiers du profil)."""
    if os.getenv("TTS_BROWSER_PROFILE"):
        return Path(os.environ["TTS_BROWSER_PROFILE"]).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "tiktok-search" / "browser-profile"


def is_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _channels(preferred: str) -> list[str | None]:
    if preferred and preferred != "auto":
        return [None if preferred == "chromium" else preferred]
    # Edge est installé sur tous les Windows ; Chrome souvent ailleurs ; sinon Chromium de Playwright.
    return ["msedge", "chrome", None] if sys.platform == "win32" else ["chrome", "msedge", None]


def _launch(pw, profile: Path, headless: bool, channel: str):
    profile.mkdir(parents=True, exist_ok=True)
    errors = []
    for ch in _channels(channel):
        try:
            return pw.chromium.launch_persistent_context(
                str(profile), channel=ch, headless=headless, viewport=None if not headless else {"width": 1280, "height": 900},
                locale="fr-FR", args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as e:  # noqa: BLE001 - navigateur absent : on essaie le suivant
            errors.append(f"{ch or 'chromium'}: {str(e).splitlines()[0]}")
    raise SyncError("Aucun navigateur utilisable (Edge, Chrome ou Chromium). Installe-en un, ou lance : "
                    "py -m playwright install chromium\n" + "\n".join(errors))


def _logged_in(ctx, base: str) -> bool:
    return any(c["name"] in SESSION_COOKIES and c.get("value") for c in ctx.cookies(base))


def _username_candidates(page) -> list[str]:
    """Ton @ vu par TikTok. Le lien « Profil » du menu donne le @ public actuel ; l'API de compte
    peut renvoyer un ancien identifiant de connexion (comptes créés avec Google, par ex.)."""
    names = []
    try:
        page.wait_for_selector('a[data-e2e="nav-profile"]', timeout=8000)
        href = page.locator('a[data-e2e="nav-profile"]').first.get_attribute("href") or ""
        if "/@" in href:
            names.append(href.split("/@", 1)[1].split("?")[0].strip("/"))
    except Exception as e:  # noqa: BLE001
        log.debug("lien de profil introuvable : %s", e)
    try:
        info = page.evaluate(
            "async () => { const r = await fetch('/passport/web/account/info/', {credentials: 'include'});"
            " return r.ok ? await r.json() : null; }")
        name = ((info or {}).get("data") or {}).get("username")
        if name:
            names.append(str(name))
    except Exception as e:  # noqa: BLE001
        log.debug("account/info indisponible : %s", e)
    return names


def _has_captcha(page) -> bool:
    return any(page.locator(sel).first.is_visible() for sel in CAPTCHA_SELECTORS if page.locator(sel).count())


def _find_favorites_tab(page, timeout: float = 20):
    """Attend que la page du profil affiche ses onglets, puis renvoie l'onglet « Favoris »."""
    return _find_tab(page, FAVORITES_TAB_TEXT, FAVORITES_TAB_SELECTORS, timeout)


def _find_tab(page, text: re.Pattern, selectors: tuple[str, ...], timeout: float = 20):
    deadline = time.monotonic() + timeout
    while True:
        candidates = [page.locator(sel).first for sel in selectors]
        candidates += [page.get_by_role("tab", name=text).first, page.get_by_text(text).first]
        for loc in candidates:
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001
                continue
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(1000)


def _visible_tabs(page) -> str:
    try:
        texts = page.locator('[role="tab"], [data-e2e$="-tab"]').all_inner_texts()
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(dict.fromkeys(t.strip() for t in texts if t.strip()))[:200]


def _open_profile_favorites(page, base: str, names: list[str], wait_captcha: Callable[[], None]) -> str:
    """Va sur ton profil (essaie chaque @ possible) et clique sur « Favoris ». Renvoie le @ retenu."""
    tried = []
    for name in dict.fromkeys(n.lstrip("@") for n in names if n):
        page.goto(f"{base}/@{name}", wait_until="domcontentloaded")
        wait_captcha()
        tab = _find_favorites_tab(page)
        if tab is not None:
            tab.click()
            return name
        tried.append(f"{page.url} (onglets vus : {_visible_tabs(page) or 'aucun'})")
    if not tried:
        raise SyncError("Impossible de trouver ton @ TikTok : renseigne TTS_TIKTOK_USERNAME dans .env.")
    raise SyncError("Onglet « Favoris » introuvable sur " + " ; ".join(tried)
                    + ". Si ce n'est pas ton profil, renseigne TTS_TIKTOK_USERNAME dans .env.")


# Demande une page de favoris depuis la page TikTok elle-même : mêmes cookies, et le script de
# sécurité de TikTok signe la requête comme pour le site. Si ça ne passe pas sans signature
# explicite, on essaie la fonction de signature du site (byted_acrawler.frontierSign).
_API_PAGE_JS = """async ([tpl, cursor, sign]) => {
  const u = new URL(tpl, location.origin);
  u.searchParams.set('cursor', String(cursor));
  for (const k of ['X-Bogus', 'X-Gnarly', '_signature']) u.searchParams.delete(k);
  let url = u.toString();
  if (sign) {
    try {
      const s = window.byted_acrawler && window.byted_acrawler.frontierSign(u.search.slice(1));
      if (s && s['X-Bogus']) url += '&X-Bogus=' + encodeURIComponent(s['X-Bogus']);
    } catch (e) {}
  }
  try {
    const r = await fetch(url, {credentials: 'include'});
    return JSON.parse(await r.text());
  } catch (e) { return null; }
}"""


def _valid_page(payload, key: str = "itemList") -> bool:
    return (isinstance(payload, dict) and payload.get("statusCode", 0) in (0, None)
            and (isinstance(payload.get(key), list) or payload.get("hasMore") is False))


def _captcha_waiter(page, interactive: bool, progress: Callable[[str], None]) -> Callable[[], None]:
    def wait_captcha() -> None:
        page.wait_for_timeout(1500)
        if not _has_captcha(page):
            return
        if not interactive:
            raise Blocked("TikTok demande une vérification (captcha).")
        progress("TikTok demande une vérification : résous-la dans la fenêtre.")
        page.wait_for_function(
            "sels => !sels.some(s => document.querySelector(s))", arg=list(CAPTCHA_SELECTORS), timeout=180_000)
    return wait_captcha


def _first_valid(page, responses: list, api: str, key: str = "itemList", timeout: float = 15,
                 match: Callable[[str], bool] = lambda url: True):
    """Attend la 1re réponse valide de ``api`` chargée par la page elle-même → (url, données) ou None."""
    for _ in range(int(timeout)):
        page.wait_for_timeout(1000)
        for resp in list(responses):
            if api not in resp.url or not match(resp.url):
                continue
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                continue
            if _valid_page(payload, key):
                return resp.url, payload
        if _has_captcha(page):
            raise Blocked("TikTok demande une vérification (captcha).")
    return None


def _replay(page, template: str, cursor, handle: Callable[[dict], bool], deadline: float,
            pace: tuple[float, float], key: str = "itemList") -> str:
    """Pages suivantes en requêtes directes depuis la page. → "done", "failed" (refusé) ou "timeout"."""
    sign = False
    while time.monotonic() < deadline:
        nxt = page.evaluate(_API_PAGE_JS, [template, cursor, sign])
        if not _valid_page(nxt, key) and not sign:
            sign = True  # 2e essai avec la signature explicite du site
            nxt = page.evaluate(_API_PAGE_JS, [template, cursor, sign])
        if not _valid_page(nxt, key):
            log.info("requêtes directes refusées (%s)", str(nxt)[:200])
            return "failed"
        if handle(nxt):
            return "done"
        cursor = nxt.get("cursor")
        page.wait_for_timeout(int(random.uniform(*pace) * 1000))
    return "timeout"


def _scroll(page, responses: list, api: str, handle: Callable[[dict], bool], deadline: float,
            key: str = "itemList") -> bool:
    """Repli : fait défiler la page et lit ce qu'elle charge. True si la fin de la liste est atteinte."""
    idle = 0
    while time.monotonic() < deadline:
        page.mouse.wheel(0, 5000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        batch = [r for r in responses if api in r.url]
        responses[:] = [r for r in responses if api not in r.url]
        got = False
        for resp in batch:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001 - réponse vide ou annulée
                continue
            if not _valid_page(payload, key):
                continue
            got = True
            if handle(payload):
                return True
        if got:
            idle = 0
            continue
        idle += 1
        if _has_captcha(page):
            raise Blocked("TikTok demande une vérification (captcha).")
        if idle >= 8:  # ~15 s sans nouvelle page : fin de liste ou page bloquée
            return False
    return False


def _collect(page, base: str, names: list[str], known: set[str], stop_on_known: bool,
             progress: Callable[[str], None], max_minutes: float, interactive: bool,
             on_username: Callable[[str], None],
             on_page: Callable[[list[FavoriteMeta], object], int] = lambda favs, cur: 0,
             resume_cursor: object = None, pace: tuple[float, float] = (0.4, 1.0)) -> SyncResult:
    responses: list = []
    page.on("response", lambda r: responses.append(r) if FAVORITES_API in r.url else None)
    username = _open_profile_favorites(page, base, names, _captcha_waiter(page, interactive, progress))
    on_username(username)
    result = SyncResult(username)
    seen: set[str] = set()
    deadline = time.monotonic() + max_minutes * 60

    def handle(payload: dict) -> bool:
        """Enregistre une page ; True s'il faut s'arrêter (fin de liste, ou plus rien de nouveau)."""
        items, has_more = parse_item_list(payload)
        new = [f for f in items if f.item.video_id not in seen]
        seen.update(f.item.video_id for f in new)
        result.favorites.extend(new)
        result.added += on_page(new, payload.get("cursor") if has_more else None)
        progress(f"{len(result.favorites)} favoris lus, {result.added} nouveaux…")
        if not has_more:
            result.complete = True
            return True
        return bool(stop_on_known and items and all(f.item.video_id in known for f in items))

    # 1re page : celle que charge la page TikTok quand on clique sur l'onglet.
    first = _first_valid(page, responses, FAVORITES_API)
    if not first:
        raise Blocked("TikTok n'a renvoyé aucun favori (liste vide, ou page bloquée).")
    responses.clear()
    template, payload = first
    if handle(payload):
        return result

    # Pages suivantes : requêtes directes (rapide, rien à afficher).
    cursor = resume_cursor if (resume_cursor not in (None, "") and not stop_on_known) else payload.get("cursor")
    if cursor != payload.get("cursor"):
        progress("Reprise de la synchro précédente…")
    state = _replay(page, template, cursor, handle, deadline, pace)
    if state != "failed":
        result.mode = "api"
        return result  # "done", ou "timeout" : la suite reprendra à la prochaine synchro
    # Repli : on fait défiler la page et on lit ce qu'elle charge.
    result.mode = "scroll"
    _scroll(page, responses, FAVORITES_API, handle, deadline)
    return result


def _items_template(list_template: str, collection_id: str) -> str:
    """Adresse de la liste d'une collection, construite à partir de celle de la liste des collections
    (mêmes paramètres de session) : secours si la page de la collection ne charge rien elle-même."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    u = urlsplit(list_template)
    q = {k: v for k, v in parse_qsl(u.query) if k not in ("X-Bogus", "X-Gnarly", "_signature", "secUid")}
    q.update({"collectionId": collection_id, "count": "30", "cursor": "0", "sourceType": "113"})
    return urlunsplit((u.scheme, u.netloc, COLLECTION_ITEMS_API + "/", urlencode(q), ""))


def _collect_collections(page, base: str, names: list[str], progress: Callable[[str], None], max_minutes: float,
                         interactive: bool, on_username: Callable[[str], None], pace: tuple[float, float],
                         on_collection: Callable[[str, str, list[FavoriteMeta], int], None]) -> SyncResult:
    """Lit tes collections (Favoris → Collections) et les vidéos de chacune."""
    from urllib.parse import quote

    responses: list = []
    page.on("response", lambda r: responses.append(r)
            if COLLECTION_LIST_API in r.url or COLLECTION_ITEMS_API in r.url else None)
    wait_captcha = _captcha_waiter(page, interactive, progress)
    username = _open_profile_favorites(page, base, names, wait_captcha)
    on_username(username)
    result = SyncResult(username)
    deadline = time.monotonic() + max_minutes * 60

    tab = _find_tab(page, COLLECTIONS_TAB_TEXT, COLLECTIONS_TAB_SELECTORS, timeout=15)
    if tab is None:
        raise SyncError("Onglet « Collections » introuvable dans tes favoris (onglets vus : "
                        f"{_visible_tabs(page) or 'aucun'}).")
    tab.click()
    first = _first_valid(page, responses, COLLECTION_LIST_API, key="collectionList")
    if not first:
        result.complete = True  # pas de collection (ou liste vide)
        return result
    list_template, payload = first
    collections: list[dict] = []

    def handle_list(p: dict) -> bool:
        collections.extend(c for c in p.get("collectionList") or [] if isinstance(c, dict))
        return not p.get("hasMore")

    if not handle_list(payload) and _replay(page, list_template, payload.get("cursor"), handle_list, deadline,
                                            pace, key="collectionList") != "done":
        _scroll(page, responses, COLLECTION_LIST_API, handle_list, deadline, key="collectionList")

    for i, coll in enumerate(collections):
        cid = str(coll.get("collectionId") or coll.get("id") or "").strip()
        name = str(coll.get("name") or "").strip() or "Sans nom"
        if not cid or time.monotonic() > deadline:
            continue
        progress(f"Collection « {name} » ({i + 1}/{len(collections)})…")
        favs: list[FavoriteMeta] = []
        seen: set[str] = set()

        def handle(p: dict) -> bool:
            items, has_more = parse_item_list(p)
            favs.extend(f for f in items if f.item.video_id not in seen)
            seen.update(f.item.video_id for f in items)
            return not has_more

        responses.clear()
        page.goto(f"{base}/@{username}/collection/{quote(name)}-{cid}", wait_until="domcontentloaded")
        wait_captcha()
        got = _first_valid(page, responses, COLLECTION_ITEMS_API, timeout=12, match=lambda url: cid in url)
        if got:
            done = handle(got[1]) or _replay(page, got[0], got[1].get("cursor"), handle, deadline, pace) == "done" \
                or _scroll(page, responses, COLLECTION_ITEMS_API, handle, deadline)
        else:
            done = _replay(page, _items_template(list_template, cid), 0, handle, deadline, pace) == "done"
        if not done and not favs:
            log.warning("collection %s (%s) illisible", name, cid)
            continue
        on_collection(cid, name, favs, i)
        result.favorites.extend(favs)
        page.wait_for_timeout(int(random.uniform(*pace) * 1000))
    result.complete = time.monotonic() <= deadline
    return result


def _debug_shot(page, debug_dir: Path | None) -> None:
    if debug_dir and page and not page.is_closed():
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(debug_dir / "sync-debug.png"), full_page=True)
            (debug_dir / "sync-debug.html").write_text(page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def run(*, profile: Path, known: set[str], stop_on_known: bool, username: str = "", login: bool = False,
        headless: bool = True, channel: str = "auto", base: str = TIKTOK, login_timeout: float = 300,
        max_minutes: float = 90, progress: Callable[[str], None] = lambda m: None,
        debug_dir: Path | None = None, on_username: Callable[[str], None] = lambda n: None,
        on_session: Callable[[bool], None] = lambda ok: None,
        on_page: Callable[[list[FavoriteMeta], object], int] = lambda favs, cur: 0,
        resume_cursor: object = None, pace: tuple[float, float] = (0.4, 1.0),
        on_collection: Callable[[str, str, list[FavoriteMeta], int], None] | None = None) -> SyncResult:
    """Ouvre le navigateur, (connexion si ``login``), puis lit les favoris (ou les collections si
    ``on_collection`` est donné)."""
    if not is_available():
        raise SyncError("Playwright n'est pas installé : pip install playwright")
    from playwright.sync_api import sync_playwright

    if not _lock.acquire(blocking=False):
        raise SyncError("Une synchronisation est déjà en cours.")
    try:
        with sync_playwright() as pw:
            ctx = _launch(pw, profile, headless=headless and not login, channel=channel)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                if login:
                    page.goto(f"{base}/login", wait_until="domcontentloaded")
                    progress("Connecte-toi dans la fenêtre qui vient de s'ouvrir.")
                    deadline = time.monotonic() + login_timeout
                    while not _logged_in(ctx, base):
                        if page.is_closed():
                            raise SyncError("Fenêtre fermée avant la connexion.")
                        if time.monotonic() > deadline:
                            raise SyncError("Connexion non terminée à temps. Réessaie.")
                        page.wait_for_timeout(1000)
                    progress("Connecté, lecture des favoris…")
                    page.wait_for_timeout(2000)
                elif not _logged_in(ctx, base):
                    on_session(False)
                    raise NotLoggedIn("Session TikTok expirée : reconnecte-toi.")
                on_session(True)
                if page.url.startswith("about:") or "/login" in page.url:
                    page.goto(base + "/", wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                names = [username] if username else []  # @ forcé dans .env, ou trouvé la dernière fois
                names += _username_candidates(page)
                if on_collection is not None:
                    return _collect_collections(page, base, names, progress, max_minutes, not headless or login,
                                                on_username, pace, on_collection)
                return _collect(page, base, names, known, stop_on_known, progress, max_minutes,
                                interactive=not headless or login, on_username=on_username,
                                on_page=on_page, resume_cursor=resume_cursor, pace=pace)
            except Exception:
                _debug_shot(page, debug_dir)
                raise
            finally:
                ctx.close()
    finally:
        _lock.release()


def forget_session(profile: Path) -> None:
    shutil.rmtree(profile, ignore_errors=True)


# --- intégration à la base ------------------------------------------------------------

class PageStore:
    """Enregistre les favoris page par page, dans l'ordre de TikTok (le plus récent d'abord)."""

    def __init__(self, db, now: float | None = None, track_resume: bool = False):
        self.db, self.now, self.index, self.track_resume = db, now or time.time(), 0, track_resume

    def __call__(self, favs: list[FavoriteMeta], next_cursor: object = None) -> int:
        added = 0
        cache = []
        pending = self.db.ids_with_status([f.item.video_id for f in favs], "pending")
        for fav in favs:
            # TikTok ne donne pas la date de mise en favori : on garde l'ordre de la liste.
            date = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.now - self.index))
            self.index += 1
            vid = fav.item.video_id
            if self.db.add_favorite(vid, fav.item.url, date, "browser:favorite", fav.kind):
                added += 1
                fields = {k: v for k, v in (("author", fav.author), ("description", fav.description),
                                            ("hashtags", fav.hashtags), ("duration", fav.duration)) if v}
                if fields:
                    self.db.save_video(vid, **fields)
                pending.add(vid)
            if vid in pending and fav.raw:
                cache.append((vid, json.dumps(fav.raw, ensure_ascii=False)))
        if cache:
            self.db.cache_put(cache, self.now)
        if self.track_resume and next_cursor not in (None, ""):
            self.db.kv_set(KV_RESUME, json.dumps(next_cursor))
        return added


def store(db, result: SyncResult, now: float | None = None) -> int:
    """Enregistre un résultat complet d'un coup (tests, imports)."""
    added = PageStore(db, now)(result.favorites)
    finish(db, result, now)
    return added


def finish(db, result: SyncResult, now: float | None = None) -> None:
    db.kv_set(KV_USERNAME, result.username)
    db.kv_set(KV_LAST_SYNC, str(now or time.time()))
    if result.complete:
        db.kv_set(KV_FULL_DONE, "1")
        db.kv_delete(KV_RESUME)


def known_ids(db) -> set[str]:
    return db.all_ids()


def account_state(db, profile: Path) -> dict:
    last = db.kv_get(KV_LAST_SYNC)
    return {
        "available": is_available(),
        "username": db.kv_get(KV_USERNAME) or "",
        "has_session": db.kv_get(KV_SESSION) == "1" and profile.is_dir(),
        "last_sync": float(last) if last else None,
        "full_done": db.kv_get(KV_FULL_DONE) == "1",
    }


def sync_into_db(db, settings, *, login: bool = False, full: bool = False,
                 progress: Callable[[str], None] = lambda m: None, base: str = TIKTOK,
                 pace: tuple[float, float] = (0.4, 1.0), on_added: Callable[[int], None] = lambda n: None) -> dict:
    """Connexion éventuelle + lecture des favoris, enregistrés au fil de l'eau. Renvoie un résumé.

    Sans fenêtre d'abord ; si TikTok demande une vérification (captcha), on recommence avec
    une fenêtre visible pour que tu puisses la résoudre."""
    profile = default_profile_dir()
    full_done = db.kv_get(KV_FULL_DONE) == "1"
    if full:
        db.kv_delete(KV_RESUME)
    stop_on_known = not full and full_done
    resume = db.kv_get(KV_RESUME) if not full_done else None
    store_page = PageStore(db, track_resume=not stop_on_known)

    def sink(favs: list[FavoriteMeta], next_cursor: object = None) -> int:
        added = store_page(favs, next_cursor)
        if added:
            on_added(added)  # l'indexation peut démarrer sans attendre la fin de la synchro
        return added
    opts = dict(profile=profile, known=known_ids(db), stop_on_known=stop_on_known,
                username=settings.tiktok_username or db.kv_get(KV_USERNAME) or "", channel=settings.browser,
                base=base, progress=progress, debug_dir=settings.tmp_dir,
                on_username=lambda n: db.kv_set(KV_USERNAME, n),
                on_session=lambda ok: db.kv_set(KV_SESSION, "1") if ok else db.kv_delete(KV_SESSION),
                on_page=sink, resume_cursor=json.loads(resume) if resume else None, pace=pace)
    headless = settings.browser_headless and not login
    try:
        result = run(login=login, headless=headless, **opts)
    except Blocked:
        if not headless:
            raise
        progress("TikTok demande une vérification : une fenêtre s'ouvre.")
        result = run(login=False, headless=False, **opts)
    finish(db, result)
    return {"username": result.username, "found": len(result.favorites), "added": result.added,
            "complete": result.complete, "mode": result.mode}


def sync_collections_into_db(db, settings, *, progress: Callable[[str], None] = lambda m: None,
                             base: str = TIKTOK, pace: tuple[float, float] = (0.4, 1.0)) -> dict:
    """Tes collections TikTok deviennent des catégories (source « tiktok »), remplacées à chaque import."""
    store_page = PageStore(db)
    seen_ids: set[str] = set()
    counts: dict[str, int] = {}

    def on_collection(cid: str, name: str, favs: list[FavoriteMeta], position: int) -> None:
        store_page(favs)  # au cas où une vidéo d'une collection ne serait pas encore dans les favoris
        cat = db.upsert_category(name, "tiktok", cid, "Collections TikTok", [], position)
        db.set_category_members(cat, [(f.item.video_id, None) for f in favs])
        seen_ids.add(cid)
        counts[name] = len(favs)

    opts = dict(profile=default_profile_dir(), known=set(), stop_on_known=False,
                username=settings.tiktok_username or db.kv_get(KV_USERNAME) or "", channel=settings.browser,
                base=base, progress=progress, debug_dir=settings.tmp_dir,
                on_username=lambda n: db.kv_set(KV_USERNAME, n),
                on_session=lambda ok: db.kv_set(KV_SESSION, "1") if ok else db.kv_delete(KV_SESSION),
                pace=pace, on_collection=on_collection)
    headless = settings.browser_headless
    try:
        result = run(headless=headless, **opts)
    except Blocked:
        if not headless:
            raise
        progress("TikTok demande une vérification : une fenêtre s'ouvre.")
        result = run(headless=False, **opts)
    if result.complete:
        db.delete_categories("tiktok", keep_external_ids=seen_ids)  # collections supprimées sur TikTok
    return {"username": result.username, "collections": counts, "complete": result.complete}
