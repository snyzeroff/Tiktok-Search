"""Synchronisation des favoris en pilotant un vrai navigateur (Playwright).

Principe :
1. ``login`` ouvre une fenêtre de navigateur sur tiktok.com : tu te connectes comme d'habitude
   (QR code avec l'appli, Google, e-mail…). La session est gardée dans un profil de navigateur
   dédié, sur ton ordinateur uniquement.
2. ``sync`` rouvre ce profil (sans fenêtre si possible), va sur ton profil, onglet « Favoris »,
   et lit les réponses JSON que la page TikTok reçoit elle-même pendant qu'on fait défiler
   (``/api/user/collect/item_list/``). Aucune requête « inventée » : on ne fait que regarder
   ce que le site charge pour afficher tes favoris.

La synchro est incrémentale : TikTok renvoie les favoris du plus récent au plus ancien, donc
après une première synchro complète on s'arrête dès qu'une page ne contient plus rien de nouveau.
"""

from __future__ import annotations

import logging
import re
import os
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


@dataclass
class SyncResult:
    username: str
    favorites: list[FavoriteMeta] = field(default_factory=list)
    complete: bool = False  # True si on est allé jusqu'au bout de la liste


# --- lecture des réponses ---------------------------------------------------------

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
    deadline = time.monotonic() + timeout
    while True:
        candidates = [page.locator(sel).first for sel in FAVORITES_TAB_SELECTORS]
        candidates += [page.get_by_role("tab", name=FAVORITES_TAB_TEXT).first,
                       page.get_by_text(FAVORITES_TAB_TEXT).first]
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


def _collect(page, base: str, names: list[str], known: set[str], stop_on_known: bool,
             progress: Callable[[str], None], max_minutes: float, interactive: bool,
             on_username: Callable[[str], None]) -> SyncResult:
    responses: list = []
    page.on("response", lambda r: responses.append(r) if FAVORITES_API in r.url else None)

    def wait_captcha() -> None:
        page.wait_for_timeout(1500)
        if not _has_captcha(page):
            return
        if not interactive:
            raise Blocked("TikTok demande une vérification (captcha).")
        progress("TikTok demande une vérification : résous-la dans la fenêtre.")
        page.wait_for_function(
            "sels => !sels.some(s => document.querySelector(s))", arg=list(CAPTCHA_SELECTORS), timeout=180_000)

    username = _open_profile_favorites(page, base, names, wait_captcha)
    on_username(username)
    result = SyncResult(username)
    seen: set[str] = set()
    deadline = time.monotonic() + max_minutes * 60
    idle = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        batch, responses[:] = responses[:], []
        got_page = False
        for resp in batch:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001 - réponse vide ou annulée
                continue
            got_page = True
            items, has_more = parse_item_list(payload)
            new = [f for f in items if f.item.video_id not in seen]
            seen.update(f.item.video_id for f in new)
            result.favorites.extend(new)
            progress(f"{len(result.favorites)} favoris lus…")
            if not has_more:
                result.complete = True
                return result
            if stop_on_known and items and all(f.item.video_id in known for f in items):
                return result  # tout le reste est déjà connu
        if got_page:
            idle = 0
        else:
            idle += 1
            if _has_captcha(page):
                raise Blocked("TikTok demande une vérification (captcha).")
            if idle >= 8:  # ~15 s sans nouvelle page : fin de liste ou page bloquée
                if not result.favorites and not seen:
                    raise Blocked("TikTok n'a renvoyé aucun favori (liste vide, ou page bloquée).")
                return result
        page.mouse.wheel(0, 5000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
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
        max_minutes: float = 20, progress: Callable[[str], None] = lambda m: None,
        debug_dir: Path | None = None, on_username: Callable[[str], None] = lambda n: None,
        on_session: Callable[[bool], None] = lambda ok: None) -> SyncResult:
    """Ouvre le navigateur, (connexion si ``login``), puis lit les favoris."""
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
                return _collect(page, base, names, known, stop_on_known, progress, max_minutes,
                                interactive=not headless or login, on_username=on_username)
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

def store(db, result: SyncResult, now: float | None = None) -> int:
    """Ajoute les nouveaux favoris (dans l'ordre de TikTok) et pré-remplit légende/auteur."""
    now = now or time.time()
    added = 0
    for i, fav in enumerate(result.favorites):
        # TikTok ne donne pas la date de mise en favori : on garde l'ordre de la liste.
        date = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - i))
        if db.add_favorite(fav.item.video_id, fav.item.url, date, "browser:favorite", fav.kind):
            added += 1
            fields = {k: v for k, v in (("author", fav.author), ("description", fav.description),
                                        ("hashtags", fav.hashtags), ("duration", fav.duration)) if v}
            if fields:
                db.save_video(fav.item.video_id, **fields)
    db.kv_set(KV_USERNAME, result.username)
    db.kv_set(KV_LAST_SYNC, str(now))
    if result.complete:
        db.kv_set(KV_FULL_DONE, "1")
    return added


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
                 progress: Callable[[str], None] = lambda m: None, base: str = TIKTOK) -> dict:
    """Connexion éventuelle + lecture des favoris + enregistrement. Renvoie un résumé.

    Sans fenêtre d'abord ; si TikTok demande une vérification (captcha), on recommence avec
    une fenêtre visible pour que tu puisses la résoudre."""
    profile = default_profile_dir()
    known = known_ids(db)
    stop_on_known = not full and db.kv_get(KV_FULL_DONE) == "1"
    opts = dict(profile=profile, known=known, stop_on_known=stop_on_known,
                username=settings.tiktok_username or db.kv_get(KV_USERNAME) or "", channel=settings.browser,
                base=base, progress=progress, debug_dir=settings.tmp_dir,
                on_username=lambda n: db.kv_set(KV_USERNAME, n),
                on_session=lambda ok: db.kv_set(KV_SESSION, "1") if ok else db.kv_delete(KV_SESSION))
    headless = settings.browser_headless and not login
    try:
        result = run(login=login, headless=headless, **opts)
    except Blocked:
        if not headless:
            raise
        progress("TikTok demande une vérification : une fenêtre s'ouvre.")
        result = run(login=False, headless=False, **opts)
    added = store(db, result)
    return {"username": result.username, "found": len(result.favorites), "added": added,
            "complete": result.complete}
