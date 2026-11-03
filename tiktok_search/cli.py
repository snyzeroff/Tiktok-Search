"""Interface en ligne de commande : tiktok-search import|links|process|search|stats|serve."""

from __future__ import annotations

import argparse
import logging
import sys

from . import importer, ocr, pipeline, transcribe
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

    s = sub.add_parser("process", help="Récupérer légendes, transcriptions et texte à l'écran")
    s.add_argument("--retry-errors", action="store_true")
    s.add_argument("--limit", type=int)

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
        items = importer.parse_export(importer.load_export(args.file), include_likes=args.likes)
        added = pipeline.import_items(db, items, "export")
        print(f"{len(items)} vidéos trouvées dans l'export, {added} nouvelles.")
        print("Lance maintenant : tiktok-search process")
    elif args.cmd == "links":
        items = importer.parse_links_text(open(args.file, encoding="utf-8").read())
        added = pipeline.import_items(db, items, "links")
        print(f"{len(items)} liens reconnus, {added} nouveaux.")
    elif args.cmd == "process":
        print(f"Transcription Whisper : {'oui' if transcribe.is_available() and settings.transcribe == 'auto' else 'non'}"
              f" · OCR : {'oui' if settings.ocr and ocr.is_available() else 'non'}")

        def progress(i: int, n: int, v: dict) -> None:
            row = db.get_video(v["id"]) or {}
            mark = "✓" if row.get("status") == "done" else "✗"
            print(f"[{i}/{n}] {mark} {v['id']} {row.get('error', '')[:80]}", flush=True)

        n = pipeline.process_all(db, settings, retry_errors=args.retry_errors, limit=args.limit,
                                 on_progress=progress)
        print(f"{n} vidéos traitées.")
    elif args.cmd == "search":
        results = search(db, " ".join(args.query), fields=args.fields, any_term=args.any, limit=args.limit)
        if not results:
            print("Aucun résultat.")
        for r in results:
            v = r.video
            print(f"\n{BOLD}@{v['author'] or '?'}{RESET}  {v['url'] if 'tiktok.com/@' in v['url'] else importer.canonical_url(v['id'])}")
            for col, snip in r.matches.items():
                print(f"  [{FIELD_LABELS[col]}] {_hl(snip)}")
    elif args.cmd == "stats":
        st = db.stats()
        print(f"{st['total']} vidéos · {st['done']} indexées · {st['pending']} en attente · "
              f"{st['error']} en erreur · {st['with_transcript']} avec transcription")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("tiktok_search.web:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
