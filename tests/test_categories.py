"""Catégories : tri automatique (propositions, aperçu, application) et filtres."""

from __future__ import annotations

import importlib

import pytest

from tiktok_search import categorize
from tiktok_search.db import Database
from tiktok_search.search import search

VIDEOS = [
    ("1", "Les meilleures pâtes carbonara", "#recette #cuisine #fyp", ""),
    ("2", "Routine gainage 10 min", "#sport #fitness #fyp", ""),
    ("3", "3 sites gratuits incroyables", "#tech #astuce", "le premier c'est un outil d'IA"),
    ("4", "Mon chat qui saute", "#chat #funny", ""),
    ("5", "Recettes de cookies faciles", "#recettes #dessert", ""),
    ("6", "Sans rapport", "#fyp", ""),
    ("7", "pas encore indexée", "#recette", ""),
]

SET = {"id": "perso", "name": "Mes thèmes", "categories": [
    {"name": "Cuisine", "keywords": ["recette", "cuisine", "dessert"]},
    {"name": "Sport", "keywords": ["sport", "gainage"]},
    {"name": "Tech", "keywords": ["tech", "ia", "outil"]},
    {"name": "Autre", "keywords": []},
]}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    for vid, desc, tags, tr in VIDEOS:
        d.add_favorite(vid, "u")
        d.save_video(vid, description=desc, hashtags=tags, transcript=tr, status="pending" if vid == "7" else "done")
    return d


def test_preview_counts_without_writing(db):
    p = categorize.preview(db, SET)
    counts = {c["name"]: c["count"] for c in p["categories"]}
    assert counts == {"Cuisine": 2, "Sport": 1, "Tech": 1, "Autre": 2} and p["total"] == 6  # « 7 » pas indexée
    assert db.list_categories() == []
    assert any("carbonara" in e for e in next(c for c in p["categories"] if c["name"] == "Cuisine")["examples"])


def test_apply_then_filter_and_reapply(db):
    categorize.apply(db, SET)
    cats = {c["name"]: c for c in db.list_categories()}
    assert {n: c["count"] for n, c in cats.items()} == {"Cuisine": 2, "Sport": 1, "Tech": 1, "Autre": 2}
    assert cats["Tech"]["keywords"] == ["tech", "ia", "outil"] and cats["Cuisine"]["set_name"] == "Mes thèmes"
    # « recettes » trouvé par le mot-clé « recette » (préfixe), transcription prise en compte (vidéo 3)
    assert {v["id"] for v in db.list_videos(category=cats["Cuisine"]["id"])} == {"1", "5"}
    assert [r.video["id"] for r in search(db, "routine", category=cats["Sport"]["id"])] == ["2"]
    assert search(db, "routine", category=cats["Cuisine"]["id"]) == []
    assert db.categories_for(["3"])["3"][0]["name"] == "Tech"
    # Nouveau tri : l'ancien est remplacé, une collection TikTok n'est pas touchée.
    tk = db.upsert_category("Ma collection", "tiktok", "777")
    db.set_category_members(tk, [("6", None)])
    categorize.apply(db, {"name": "Deux", "categories": [{"name": "Animaux", "keywords": ["chat"]}]})
    names = {c["name"] for c in db.list_categories()}
    assert names == {"Animaux", "Autre", "Ma collection"}
    assert categorize.saved_set(db)["name"] == "Deux"
    categorize.clear(db)
    assert [c["name"] for c in db.list_categories()] == ["Ma collection"] and categorize.saved_set(db) is None


def test_normalize_set_cleans_user_and_ai_input():
    s = categorize.normalize_set({"name": "x", "categories": [
        {"name": "  Cuisine  ", "keywords": "Recette, #Cuisine , , recette"},
        {"name": "cuisine", "keywords": ["doublon"]},
        {"name": "", "keywords": ["vide"]},
        "pas un dict",
        {"name": "Signes", "keywords": ['"(*)"', "c++", "ok"]},
    ]})
    assert [c["name"] for c in s["categories"]] == ["Cuisine", "Signes"]
    assert s["categories"][0]["keywords"] == ["recette", "cuisine"]
    assert categorize.normalize_set({"categories": []}) is None
    assert categorize.preview(Database(":memory:"), {"categories": [{"name": "Signes", "keywords": ['"(*)"']}]})


def test_hashtag_set_skips_generic_tags(db):
    for i in range(3):
        for tag in ("#recette #dessert #fyp", "#sport #gym #viral", "#voyage #travel #foryou"):
            vid = f"h{i}{tag[1:4]}"
            db.add_favorite(vid, "u")
            db.save_video(vid, status="done", hashtags=tag)
    s = categorize.hashtag_set(db)
    names = [c["name"] for c in s["categories"]]
    assert "Recette" in names and "Sport" in names and "Voyage" in names and names[-1] == "Autre"
    assert not {"Fyp", "Viral", "Foryou"} & set(names)
    # voisins habituels comme mots-clés (« cuisine ») ; un hashtag déjà catégorie n'est pas répété
    recette = next(c for c in s["categories"] if c["name"] == "Recette")["keywords"]
    assert recette[0] == "recette" and "cuisine" in recette and "dessert" not in recette


def test_propose_with_ai_and_fallback(db, monkeypatch):
    from tiktok_search import analyze

    class S:
        llm_provider = "gemini"

    for i in range(12):
        db.add_favorite(f"x{i}", "u")
        db.save_video(f"x{i}", status="done", description=f"vidéo {i}", hashtags="#test")
    monkeypatch.setattr(analyze, "llm_json", lambda s, prompt: {"sets": [
        {"name": "Par sujet", "categories": [{"name": "A", "keywords": ["a"]}, {"name": "B", "keywords": ["b"]},
                                              {"name": "C", "keywords": ["c"]}]},
        {"name": "Trop court", "categories": [{"name": "A", "keywords": []}]}]})
    r = categorize.propose(db, S())
    assert r["llm_error"] is None and r["sets"][0]["name"] == "Par sujet" and r["sets"][0]["by_ai"]
    assert [x["id"] for x in r["sets"]][-2:] == ["themes", "usage"]
    monkeypatch.setattr(analyze, "llm_json", lambda s, prompt: (_ for _ in ()).throw(analyze.LLMError("quota")))
    r = categorize.propose(db, S())
    assert r["llm_error"] == "quota" and r["sets"][0]["id"] in ("hashtags", "themes")


def test_presets_find_matches_on_realistic_captions(tmp_path):
    db = Database(tmp_path / "p.db")
    rows = {"a": ("Mon setup gaming", "#gaming #ps5"), "b": ("Recette de crêpes", "#cuisine"),
            "c": ("Workout jambes", "#fitness"), "d": ("Astuce Excel", "#tuto #astuce")}
    for vid, (d, t) in rows.items():
        db.add_favorite(vid, "u")
        db.save_video(vid, status="done", description=d, hashtags=t)
    themes = {c["name"]: c["count"] for c in categorize.preview(db, categorize.PRESETS[0])["categories"]}
    assert themes["Jeux vidéo"] == 1 and themes["Cuisine & recettes"] == 1 and themes["Sport & fitness"] == 1
    usage = {c["name"]: c["count"] for c in categorize.preview(db, categorize.PRESETS[1])["categories"]}
    assert usage["Tutoriels & astuces"] >= 1


# --- API -------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("TTS_LLM", "none")
    from fastapi.testclient import TestClient

    import tiktok_search.web as web
    web = importlib.reload(web)
    for vid, desc, tags, tr in VIDEOS:
        web.db.add_favorite(vid, "u")
        web.db.save_video(vid, description=desc, hashtags=tags, transcript=tr, status="done")
    return TestClient(web.app, base_url="http://localhost"), web


def test_categories_api_flow(client):
    c, web = client
    h = {"X-TTS": "1"}
    sets = c.post("/api/categories/propose", headers=h).json()["sets"]
    assert {"themes", "usage"} <= {s["id"] for s in sets}
    prev = c.post("/api/categories/preview", headers=h, json=SET).json()
    assert {x["name"]: x["count"] for x in prev["categories"]}["Cuisine"] == 3
    assert c.post("/api/categories/apply", headers=h, json=SET).status_code == 200
    cats = c.get("/api/categories").json()
    cuisine = next(x for x in cats["categories"] if x["name"] == "Cuisine")
    assert cats["auto_set"]["name"] == "Mes thèmes" and cuisine["count"] == 3
    res = c.get(f"/api/videos?category={cuisine['id']}").json()["results"]
    assert {r["id"] for r in res} == {"1", "5", "7"} and res[0]["categories"][0]["name"] == "Cuisine"
    found = c.get(f"/api/search?q=cookies&category={cuisine['id']}").json()["results"]
    assert [r["id"] for r in found] == ["5"] and found[0]["categories"][0]["source"] == "auto"
    assert c.post("/api/categories/apply", headers=h, json={"categories": []}).status_code == 400
    assert c.post("/api/categories/apply", json=SET).status_code == 403  # anti-CSRF
    assert c.post("/api/categories/tiktok", headers=h).status_code == 400  # compte pas connecté
    assert c.post("/api/categories/clear", headers=h).json()["deleted"] == 4
