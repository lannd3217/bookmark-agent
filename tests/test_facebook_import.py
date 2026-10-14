from pathlib import Path

import pytest

from app.importers.facebook import parse_facebook_saves

FIXTURE = Path(__file__).parent / "fixtures" / "facebook_saves.json"


def test_parses_fixture_skipping_missing_uri_and_duplicates():
    assert parse_facebook_saves(FIXTURE) == [
        {
            "url": "https://example.com/article?id=7",
            "title": "A normal article",
            "created_at": "2023-11-14T22:13:20+00:00",
        }
    ]


def test_keeps_first_occurrence_of_duplicate(tmp_path):
    f = tmp_path / "saved_items.json"
    f.write_text(
        '{"saves_v2": ['
        '{"timestamp": 1, "data": [{"uri": "https://a.com/?utm_medium=x", "title": "first"}]},'
        '{"timestamp": 2, "data": [{"uri": "https://b.com/", "title": "other"},'
        '                          {"uri": "https://a.com/", "title": "second"}]}'
        "]}"
    )
    items = parse_facebook_saves(f)
    assert [(i["url"], i["title"]) for i in items] == [
        ("https://a.com/", "first"),
        ("https://b.com/", "other"),
    ]
    assert items[0]["created_at"] == "1970-01-01T00:00:01+00:00"


def test_malformed_json_raises_value_error(tmp_path):
    f = tmp_path / "saved_items.json"
    f.write_text('{"saves_v2": [')
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_facebook_saves(f)


def test_missing_saves_v2_raises_value_error(tmp_path):
    f = tmp_path / "saved_items.json"
    f.write_text('{"saves": []}')
    with pytest.raises(ValueError, match="saves_v2"):
        parse_facebook_saves(f)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        parse_facebook_saves(tmp_path / "nope.json")
