# TikTok Search

Retrouve n'importe quelle **vidéo ou carrousel** de tes **favoris TikTok** en tapant un ou plusieurs
mots-clés, va **directement au bon moment** (timecode) ou à la **bonne page**, et **analyse** une ou
plusieurs publications pour en extraire outils, sites, produits, comptes, étapes et codes promo.

## Fonctionnalités

- 🔎 **Recherche** dans la légende, les hashtags, la transcription, le texte à l'écran, l'auteur
  et les outils/sites détectés par l'analyse — sans accents ni casse, expression exacte, exclusion, ET/OU,
  filtres vidéos/carrousels, tri pertinence/date.
- ⏱ **Timecodes cliquables** sous chaque résultat (« ▶ 0:42 … extrait … ») : la vidéo s'ouvre et saute au
  moment où le mot est prononcé (▶) ou affiché à l'écran (🔤).
- 🖼 **Carrousels** : images enregistrées en local, numéros de page cliquables (« Page 3 »), visionneuse
  avec flèches et clavier ←/→, OCR page par page.
- 📝 **Transcription complète** dans la fenêtre de lecture, mots recherchés **surlignés**, chaque phrase
  cliquable, phrase en cours mise en évidence pendant la lecture, « aller au passage suivant », copier.
- 🤖 **Analyse IA** d'une ou plusieurs publications (cases à cocher) : résumé, points clés, étapes, outils,
  sites, produits, comptes, codes promo — chacun avec le timecode/la page où il apparaît. Export
  **Markdown / CSV / JSON**. Les noms trouvés deviennent cherchables.
- 📸 **Smart screens** : au lieu d'envoyer la vidéo à l'IA, on garde seulement les captures *réellement
  différentes* (empreinte perceptuelle), réduites à 768 px.

## Coût : pensé pour être (presque) gratuit

| Étape | Coût |
|---|---|
| Légende, hashtags, sous-titres TikTok, images de carrousel | gratuit (lecture de la page TikTok) |
| Transcription | gratuit (Whisper en local, seulement si TikTok n'a pas de sous-titres) |
| Texte à l'écran | gratuit (Tesseract en local) |
| Liens, domaines (même prononcés : « notion point so »), @comptes, codes promo, prix | gratuit (extraction locale) |
| Analyse IA mode **texte** (défaut) | quelques milliers de tokens → **palier gratuit Gemini**, ou 0 € avec **Ollama** |
| Analyse IA mode **smart screens** | + ~8 petites images, seulement quand tu le demandes (ou en mode auto si peu de texte) |
| Ré-analyse | **0 €** : résultats en cache tant que le contenu ne change pas ; la fusion multi-vidéos est locale |

Pour l'IA : crée une clé gratuite sur [Google AI Studio](https://aistudio.google.com/apikey) et mets-la
dans `GEMINI_API_KEY`, ou installe [Ollama](https://ollama.com) avec un modèle vision
(`ollama pull qwen2.5vl:7b`) et `TTS_LLM=ollama`. Sans IA, l'extraction locale fonctionne quand même.

## Ce qui est indexé

La recherche porte sur :

| Champ | Source |
|---|---|
| **Légende** (description) et **hashtags** | page TikTok (repli yt-dlp, puis oEmbed) |
| **Transcription** (ce qui est dit) | sous-titres TikTok s'ils existent, sinon **Whisper** en local |
| **Texte à l'écran** | OCR (Tesseract) sur les smart screens d'une vidéo, ou sur chaque page d'un carrousel |
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
tiktok-search process --all                 # tout réindexer (ex. après mise à jour)
tiktok-search search recette pâtes          # tous les mots (ET)
tiktok-search search '"plan de travail"'    # expression exacte
tiktok-search search carbonara gainage --any       # au moins un mot (OU)
tiktok-search search astuce -pub --in transcript   # exclusion + un seul champ
tiktok-search analyze 7400000000000000001 https://www.tiktok.com/@x/video/74…   # → Markdown
tiktok-search analyze <ids…> --mode screens --format csv > outils.csv
tiktok-search stats
```

## Lecture et timecodes

Par défaut, la vidéo est lue dans le **lecteur officiel TikTok** intégré, à qui l'on envoie la commande
`seekTo` au clic sur un timecode. Si ton navigateur bloque ce lecteur, ou pour un saut toujours précis,
active `TTS_KEEP_VIDEO=on` : la vidéo est alors gardée en local (≈ 2-5 Mo par vidéo en 720p) et lue
par le lecteur du navigateur.

## Architecture

```
tiktok_search/
  importer.py      lecture de l'export TikTok (JSON/zip, plusieurs formats) et des liens
  tiktok_oauth.py  Login Kit OAuth + Data Portability API
  tiktok_page.py   lecture de la page TikTok : vidéo ou carrousel, sous-titres horodatés, images
  enrich.py        sous-titres VTT → segments, replis yt-dlp/oEmbed, téléchargement, audio
  screens.py       smart screens (échantillonnage + dHash + dédoublonnage)
  transcribe.py    faster-whisper avec horodatage (optionnel)
  ocr.py           Tesseract (optionnel)
  analyze.py       extraction locale + IA (Gemini / Ollama), cache, fusion multi-vidéos, export
  pipeline.py      orchestration de l'indexation
  db.py            SQLite + FTS5 (vidéos, segments horodatés, analyses), migration auto
  search.py        requêtes, BM25, extraits surlignés, timecodes/pages correspondants
  web.py           API FastAPI ;  static/index.html  interface
  cli.py           commande `tiktok-search`
```

Toutes les données restent en local dans `data/tiktok_search.db` ; les vidéos téléchargées pour la
transcription sont supprimées juste après traitement.

## Pistes pour la suite

- **Recherche sémantique** (embeddings multilingues) : trouver « recette italienne » même si la vidéo dit « carbonara ».
- **Questions en langage naturel** sur ses favoris (« la vidéo où un coach montre un exercice pour le dos »).
- Analyse automatique en tâche de fond de tous les favoris (en respectant les quotas gratuits).
- **Multi-utilisateurs** (comptes, une base par utilisateur) pour un déploiement en ligne.
- Synchronisation périodique automatique des nouveaux favoris.

> ⚠️ Respecte les conditions d'utilisation de TikTok : l'outil est conçu pour un usage personnel sur
> ses propres favoris. Le téléchargement via yt-dlp peut casser quand TikTok modifie son site
> (`pip install -U yt-dlp`).

## Robustesse

- **TikTok qui bloque** (429/403/captcha) : pauses croissantes (30 s → 10 min), puis arrêt propre.
  Les vidéos restantes sont gardées en attente, et un bouton « Arrêter » est disponible.
- **Vidéo supprimée ou privée** : marquée « indisponible » et non retentée en boucle.
- **Erreurs réseau passagères** : nouvelles tentatives automatiques. Téléchargements plafonnés
  en taille et écrits de façon atomique (jamais de fichier à moitié écrit).
- **Échec partiel** : si la transcription ou l'OCR échoue, la légende et les hashtags sont quand
  même indexés, et l'avertissement est conservé.
- **Repli dégradé** (yt-dlp, oEmbed) : n'écrase jamais des données déjà indexées.
- **Import** : JSON *ou* TXT, `.zip` ou fichier seul, liens courts `vm.tiktok.com`, protection
  contre les archives piégées, limite de 1 Go.
- **IA** : réponse validée et normalisée, délai de nouvelle tentative de Gemini respecté, résultat
  invalide jamais mis en cache. L'export et l'affichage d'une analyse existante ne sont jamais
  facturés.
- **Sécurité du serveur local** : protection CSRF (en-tête obligatoire) et DNS rebinding (hôtes
  autorisés), en-têtes CSP et anti-iframe, jeton d'accès optionnel (`TTS_AUTH_TOKEN`), échappement
  HTML partout, export CSV protégé contre l'injection de formules.
- **Base SQLite en mode WAL** : recherche fluide pendant l'indexation, mise à jour automatique du
  schéma sans perte.

Ordres de grandeur mesurés sur 5 000 favoris (60 000 phrases horodatées) : indexation en base
≈ 0,7 ms par vidéo, recherche avec timecodes en 10 à 50 ms.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
