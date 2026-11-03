import io
import json
import zipfile

import pytest

from tiktok_search import importer
from tiktok_search.db import Database
from tiktok_search.enrich import parse_vtt
from tiktok_search.search import build_match_query, search

EXPORT = {
    "Activity": {
        "Favorite Videos": {
            "FavoriteVideoList": [
                {"Date": "2024-05-01 10:00:00", "Link": "https://www.tiktokv.com/share/video/7364000000000000001/"},
                {"Date": "2024-05-02 10:00:00", "Link": "https://www.tiktok.com/@chef/video/7364000000000000002"},
            ]
        },
        "Like List": {
            "ItemFavoriteList": [
                {"date": "2024-04-01 10:00:00", "link": "https://www.tiktokv.com/share/video/7364000000000000003/"},
                {"date": "2024-04-01 10:00:00", "link": "https://www.tiktokv.com/share/video/7364000000000000001/"},
            ]
        },
        "Video Browsing History": {"VideoList": [{"Date": "x", "Link": "https://www.tiktokv.com/share/video/7364000000000000009/"}]},
    }
}


def test_parse_export_favorites_only():
    items = importer.parse_export(EXPORT)
    assert sorted(i.video_id for i in items) == ["7364000000000000001", "7364000000000000002"]
    assert all(i.kind == "favorite" for i in items)


def test_parse_export_with_likes_keeps_favorite_kind():
    items = {i.video_id: i for i in importer.parse_export(EXPORT, include_likes=True)}
    assert set(items) == {"7364000000000000001", "7364000000000000002", "7364000000000000003"}
    assert items["7364000000000000001"].kind == "favorite"
    assert items["7364000000000000003"].kind == "like"


def test_parse_newer_layout_and_zip():
    data = {"Your Activity": {"Favorite Videos": {"FavoriteVideoList": [
        {"Date": "2025-01-01", "Link": "https://www.tiktok.com/@a/video/7400000000000000000"}]}}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("TikTok_Data/user_data_tiktok.json", json.dumps(data))
    items = importer.parse_export(importer.load_export_bytes(buf.getvalue()))
    assert [i.video_id for i in items] == ["7400000000000000000"]


def test_parse_links_text():
    text = "https://www.tiktok.com/@x/video/7300000000000000000?lang=fr\nn'importe quoi\nhttps://www.tiktok.com/@x/video/7300000000000000000"
    assert [i.video_id for i in importer.parse_links_text(text)] == ["7300000000000000000"]


def test_parse_vtt():
    vtt = "WEBVTT\n\n00:00.000 --> 00:01.000\nBonjour à tous\n\n00:01.000 --> 00:02.000\nBonjour à tous\n\n00:02.000 --> 00:03.000\n<c>voici la recette</c>\n"
    assert parse_vtt(vtt) == "Bonjour à tous voici la recette"


@pytest.mark.parametrize("q,expected", [
    ("pates", '"pates"*'),
    ("recette pates", '"recette" AND "pates"*'),
    ('"plan de travail"', '"plan de travail"'),
    ("#astuce -pub", '("astuce") NOT ("pub")'),
    ('a OR) "', '"a" AND "OR"'),
    ("", ""),
])
def test_build_match_query(q, expected):
    assert build_match_query(q) == expected


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.add_favorite("1", "u1")
    d.save_video("1", author="chef", description="Recette de pâtes carbonara #cuisine", hashtags="#cuisine",
                 transcript="aujourd'hui on fait une vraie carbonara sans crème", status="done")
    d.add_favorite("2", "u2")
    d.save_video("2", author="coach", description="Routine du matin", hashtags="#sport",
                 transcript="dix minutes de gainage", ocr_text="ÉTAPE 1 : GAINAGE", status="done")
    return d


def test_search_ignores_accents_and_case(db):
    assert [r.video["id"] for r in search(db, "PATES")] == ["1"]
    assert [r.video["id"] for r in search(db, "etape")] == ["2"]


def test_search_transcript_and_fields(db):
    res = search(db, "creme")
    assert [r.video["id"] for r in res] == ["1"]
    assert set(res[0].matches) == {"transcript"}
    assert search(db, "creme", fields=["description"]) == []
    assert [r.video["id"] for r in search(db, "gainage", fields=["ocr_text"])] == ["2"]


def test_search_phrase_any_and_exclude(db):
    assert [r.video["id"] for r in search(db, '"vraie carbonara"')] == ["1"]
    assert search(db, '"carbonara vraie"') == []
    assert {r.video["id"] for r in search(db, "carbonara gainage", any_term=True)} == {"1", "2"}
    assert search(db, "carbonara gainage") == []
    assert search(db, "carbonara -creme") == []


def test_save_video_reindexes(db):
    db.save_video("1", transcript="autre chose")
    assert [r.video["id"] for r in search(db, "creme")] == []
    assert db.stats()["done"] == 2
