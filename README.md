# TikTok Search

Retrouve n'importe quelle vidéo de tes **favoris TikTok** en tapant un ou plusieurs mots-clés.
La recherche porte sur :

| Champ | Source |
|---|---|
| **Légende** (description) et **hashtags** | métadonnées de la vidéo (yt-dlp, repli oEmbed) |
| **Transcription** (ce qui est dit) | sous-titres TikTok s'ils existent, sinon **Whisper** en local |
| **Texte à l'écran** | OCR (Tesseract) sur une image toutes les 2 s |
| **Auteur** | métadonnées |

La recherche ignore les accents et la casse (`pates` trouve « Pâtes »), classe les résultats par
pertinence (BM25, légende et hashtags comptent plus) et affiche l'extrait où le mot apparaît.

## Démarrage rapide

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                      # base : légendes, hashtags, sous-titres TikTok
pip install -e '.[transcription]'     # + transcription Whisper locale (recommandé)
pip install -e '.[ocr]'               # + OCR (installer aussi tesseract : apt install tesseract-ocr tesseract-ocr-fra)
cp .env.example .env                  # puis ajuster si besoin
tiktok-search serve                   # → http://localhost:8000
```

`ffmpeg` doit être installé pour la transcription et l'OCR.

## Récupérer ses favoris

TikTok **ne donne pas accès aux favoris via son API publique classique**. Trois façons sont prévues :

1. **Importer l'export TikTok** (marche pour tout le monde, aujourd'hui) :
   TikTok → Profil → ☰ → Paramètres et confidentialité → Compte → *Télécharger tes données* →
   format **JSON** → *Demander les données*. Quand le fichier est prêt (quelques heures à quelques jours),
   dépose le `.zip` dans l'interface ou :
   ```bash
   tiktok-search import ~/Téléchargements/TikTok_Data.zip   # --likes pour inclure les vidéos likées
   ```
2. **Connecter son compte TikTok** (bouton « Connecter TikTok ») via la *Data Portability API* :
   c'est la seule voie officielle et automatisée. Elle est réservée aux utilisateurs de l'**EEE / Royaume-Uni**
   et nécessite une application approuvée sur [developers.tiktok.com](https://developers.tiktok.com)
   (renseigner `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET`). L'export est asynchrone : après connexion,
   cliquer plus tard sur « Vérifier / importer l'export ». Voir `tiktok_search/tiktok_oauth.py` —
   les endpoints sont à valider contre la doc TikTok avant production.
3. **Coller des liens** de vidéos, un par ligne (`tiktok-search links liens.txt`).

## Ligne de commande

```bash
tiktok-search process                       # indexe les vidéos en attente
tiktok-search process --retry-errors
tiktok-search search recette pâtes          # tous les mots (ET)
tiktok-search search '"plan de travail"'    # expression exacte
tiktok-search search carbonara gainage --any       # au moins un mot (OU)
tiktok-search search astuce -pub --in transcript   # exclusion + un seul champ
tiktok-search stats
```

## Architecture

```
tiktok_search/
  importer.py      lecture de l'export TikTok (JSON/zip, plusieurs formats) et des liens
  tiktok_oauth.py  Login Kit OAuth + Data Portability API
  enrich.py        yt-dlp : métadonnées, sous-titres, téléchargement, audio, images
  transcribe.py    faster-whisper (optionnel)
  ocr.py           Tesseract (optionnel)
  pipeline.py      orchestration de l'indexation
  db.py            SQLite + index FTS5 (unicode61, sans accents)
  search.py        requêtes, classement BM25, extraits surlignés
  web.py           API FastAPI ;  static/index.html  interface
  cli.py           commande `tiktok-search`
```

Toutes les données restent en local dans `data/tiktok_search.db` ; les vidéos téléchargées pour la
transcription sont supprimées juste après traitement.

## Pistes pour la suite

- **Recherche sémantique** (embeddings multilingues) : trouver « recette italienne » même si la vidéo dit « carbonara ».
- **Questions en langage naturel** sur ses favoris avec un LLM (« la vidéo où un coach montre un exercice pour le dos »).
- **Multi-utilisateurs** (comptes, une base par utilisateur) pour un déploiement en ligne.
- Synchronisation périodique automatique des nouveaux favoris.

> ⚠️ Respecte les conditions d'utilisation de TikTok : l'outil est conçu pour un usage personnel sur
> ses propres favoris. Le téléchargement via yt-dlp peut casser quand TikTok modifie son site
> (`pip install -U yt-dlp`).

## Tests

```bash
pip install -e '.[dev]' && pytest
```
