"""Interface en ligne de commande : tiktok-search login|sync|import|links|process|search|stats|serve."""

from __future__ import annotations

import argparse
import logging
import sys

from . import analyze, browser_sync, importer, ocr, pipeline, transcribe
from .config import get_settings
from .db import Database
from .search import FIELD_LABELS, MARK_CLOSE, MARK_OPEN, search

BOLD, RESET = "\033[1;33m", "\033[0m"


def _hl(s: str) -> str:
    return s.replace(MARK_OPEN, BOLD).replace(MARK_CLOSE, RESET)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tiktok-search", description="Recherche dans tes favoris TikTok.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="Importer l'export TikTok (.json ou .zip)")
    s.add_argument("file")
    s.add_argument("--likes", action="store_true", help="Inclure aussi les vidéos likées")

    s = sub.add_parser("links", help="Importer une liste de liens (fichier texte, un lien par ligne)")
    s.add_argument("file")

    sub.add_parser("login", help="Se connecter à TikTok dans une fenêtre de navigateur, puis récupérer les favoris")
    s = sub.add_parser("sync", help="Récupérer les nouveaux favoris du compte connecté")
    s.add_argument("--full", action="store_true", help="Relire toute la liste des favoris")
    s.add_argument("--show", action="store_true", help="Afficher la fenêtre du navigateur")

    s = sub.add_parser("process", help="Récupérer légendes, transcriptions et texte à l'écran")
    s.add_argument("--retry-errors", action="store_true")
    s.add_argument("--all", action="store_true", help="Tout réindexer (après une mise à jour par ex.)")
    s.add_argument("--limit", type=int)

    s = sub.add_parser("analyze", help="Analyser une ou plusieurs vidéos (outils, sites, produits…)")
    s.add_argument("ids", nargs="+", help="ID ou liens des vidéos")
    s.add_argument("--mode", choices=["text", "screens", "auto"], default="text",
                   help="text = le moins cher ; screens = + captures d'écran sélectionnées")
    s.add_argument("--force", action="store_true", help="Ignorer le cache")
    s.add_argument("--format", choices=["md", "csv", "json"], default="md")

    s = sub.add_parser("search", help="Rechercher un ou plusieurs mots-clés")
    s.add_argument("query", nargs="+")
    s.add_argument("--in", dest="fields", choices=list(FIELD_LABELS), action="append",
                   help="Limiter à un champ (répétable)")
    s.add_argument("--any", action="store_true", help="Au moins un des mots (OU) au lieu de tous (ET)")
    s.add_argument("-n", "--limit", type=int, default=20)

    sub.add_parser("stats", help="État de l'index")

    s = sub.add_parser("serve", help="Lancer l'interface web")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = get_settings()
    db = Database(settings.db_path)

    if args.cmd == "import":
        try:
            items = importer.parse_export(importer.load_export(args.file), include_likes=args.likes)
        except (OSError, ValueError) as e:
            print(f"Import impossible : {e}", file=sys.stderr)
            return 1
        added = pipeline.import_items(db, items, "export")
        print(f"{len(items)} vidéos trouvées dans l'export, {added} nouvelles.")
        print("Lance maintenant : tiktok-search process")
    elif args.cmd == "links":
        with open(args.file, encoding="utf-8", errors="replace") as f:
            items = importer.parse_links_text(importer.resolve_short_links(f.read()))
        added = pipeline.import_items(db, items, "links")
        print(f"{len(items)} liens reconnus, {added} nouveaux.")
    elif args.cmd in ("login", "sync"):
        if args.cmd == "sync" and args.show:
            settings.browser_headless = False
        try:
            r = browser_sync.sync_into_db(db, settings, login=args.cmd == "login",
                                          full=getattr(args, "full", False), progress=print)
        except browser_sync.SyncError as e:
            print(f"Synchro impossible : {e}", file=sys.stderr)
            print(f"(capture de la page : {settings.tmp_dir / 'sync-debug.png'})", file=sys.stderr)
            return 1
        print(f"@{r['username']} : {r['found']} favoris lus, {r['added']} nouveaux."
              + ("" if r["complete"] or not r["found"] else " Liste pas encore lue en entier : relance la synchro, elle reprendra."))
        if r["added"]:
            print("Lance maintenant : tiktok-search process")
    elif args.cmd == "process":
        if args.all:
            print(f"{db.reset_all()} vidéos remises en attente.")
        print(f"Transcription Whisper : {'oui' if transcribe.is_available() and settings.transcribe == 'auto' else 'non'}"
              f" · OCR : {'oui' if settings.ocr and ocr.is_available() else 'non'}")

        if settings.ocr and not pipeline.ffmpeg_available():
            print("⚠️  ffmpeg introuvable : pas d'OCR.")
        marks = {"done": "✓", "unavailable": "∅", "error": "✗"}

        def progress(i: int, n: int, v: dict, status: str) -> None:
            row = db.get_video(v["id"]) or {}
            print(f"[{i}/{n}] {marks.get(status, '?')} {v['id']} {row.get('error', '')[:80]}", flush=True)

        job = pipeline.Job()
        try:
            n = pipeline.process_all(db, settings, retry_errors=args.retry_errors, limit=args.limit,
                                     on_progress=progress, job=job)
        except KeyboardInterrupt:
            job.cancel = True
            print("\nArrêt demandé : les vidéos non traitées restent en attente.")
            return 130
        if job.message:
            print(job.message)
        print(f"{n} vidéos traitées.")
    elif args.cmd == "analyze":
        ids = [importer.extract_video_id(x) or x for x in args.ids]
        data = analyze.analyze_many(db, settings, ids, args.mode, args.force)
        for r in data["results"]:
            if r["error"]:
                print(f"⚠️  {r['video_id']} : {r['error']}", file=sys.stderr)
        if args.format == "json":
            import json

            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(analyze.to_csv(data["aggregate"]) if args.format == "csv" else analyze.to_markdown(data["aggregate"]))
    elif args.cmd == "search":
        results = search(db, " ".join(args.query), fields=args.fields, any_term=args.any, limit=args.limit)
        if not results:
            print("Aucun résultat.")
        for r in results:
            v = r.video
            print(f"\n{BOLD}@{v['author'] or '?'}{RESET}  {importer.canonical_url(v['id'], v['author'] or '', v['kind'] or 'video')}")
            for col, snip in r.matches.items():
                if col in ("transcript", "ocr_text") and r.hits:
                    continue
                print(f"  [{FIELD_LABELS[col]}] {_hl(snip)}")
            for h in r.hits[:8]:
                where = f"page {h['page']}" if h["page"] else analyze.fmt_time(h["start"])
                icon = "🖼" if h["page"] else ("🔤" if h["kind"] == "ocr" else "▶")
                print(f"    {icon} {where:>7}  {_hl(h['text'])}")
    elif args.cmd == "stats":
        st = db.stats()
        print(f"{st['total']} favoris ({st['videos']} vidéos, {st['carousels']} carrousels) · {st['done']} indexés · "
              f"{st['pending']} en attente · {st['error']} en erreur · {st['unavailable']} indisponibles · "
              f"{st['with_transcript']} transcrits · {st['analysed']} analysés")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("tiktok_search.web:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
