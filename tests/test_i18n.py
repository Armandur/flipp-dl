"""Tests for gettext-based i18n: catalog lookup, fallback and web-facing
language switching (session, ``<html lang>``, rendered strings)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.web.app import create_app
from flipp_dl.web.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_language,
    normalize_language,
    translate,
)

# ---------------------------------------------------------------------------
# translate() / normalize_language() - unit tests
# ---------------------------------------------------------------------------


def test_normalize_language_accepts_supported():
    assert normalize_language("sv") == "sv"
    assert normalize_language("en") == "en"


@pytest.mark.parametrize("value", [None, "", "de", "sv-SE", "SV"])
def test_normalize_language_falls_back_to_default(value):
    assert normalize_language(value) == DEFAULT_LANGUAGE


def test_translate_swedish_catalog_hit():
    assert translate("sv", "Dashboard") == "Översikt"


def test_translate_falls_back_to_msgid_when_untranslated():
    # No catalog entry exists for this string in any language - the
    # original text must come back unchanged, never a blank string or a
    # raw message key.
    assert translate("sv", "This string is not in any catalog") == (
        "This string is not in any catalog"
    )


def test_translate_english_is_the_msgid_itself():
    # English has no catalog at all - it *is* the source text.
    assert translate("en", "Dashboard") == "Dashboard"


def test_translate_unsupported_language_falls_back_to_english():
    assert translate("de", "Dashboard") == "Dashboard"


def test_supported_languages_include_english_and_swedish():
    assert set(SUPPORTED_LANGUAGES) == {"en", "sv"}


# ---------------------------------------------------------------------------
# Web-facing behaviour
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)
    app = create_app(db_path=tmp_path / "flipp.db", output_root=tmp_path / "output")
    return TestClient(app)


def test_dashboard_defaults_to_english(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<html lang="en">' in resp.text
    assert "Dashboard" in resp.text
    assert "Översikt" not in resp.text


def test_language_switch_sets_session_and_redirects(client: TestClient):
    resp = client.get("/language/sv?next=/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_dashboard_in_swedish_after_switch(client: TestClient):
    client.get("/language/sv?next=/")
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<html lang="sv">' in resp.text
    assert "Översikt" in resp.text
    # The nav must be consistently translated, not a mix of languages.
    assert "Publikationer" in resp.text
    assert "Bibliotek" in resp.text
    assert "Jobb" in resp.text
    assert "Inställningar" in resp.text
    # The browser tab title is UI text too - it must not stay hardcoded
    # English while the rest of the page switched to Swedish.
    assert "<title>Översikt – Flipp-DL</title>" in resp.text
    assert "<title>Dashboard – Flipp-DL</title>" not in resp.text


def test_page_titles_translate_across_pages(client: TestClient):
    client.get("/language/sv?next=/")
    cases = [
        ("/publications", "<title>Publikationer – Flipp-DL</title>"),
        ("/jobs", "<title>Jobb – Flipp-DL</title>"),
        ("/settings", "<title>Inställningar – Flipp-DL</title>"),
        ("/library", "<title>Bibliotek – Flipp-DL</title>"),
    ]
    for path, expected_title in cases:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert expected_title in resp.text, path


def test_switching_back_to_english_restores_english(client: TestClient):
    client.get("/language/sv?next=/")
    client.get("/language/en?next=/")
    resp = client.get("/")
    assert '<html lang="en">' in resp.text
    assert "Dashboard" in resp.text


def test_unknown_language_code_falls_back_to_english(client: TestClient):
    client.get("/language/fr?next=/")
    resp = client.get("/")
    assert '<html lang="en">' in resp.text


def test_language_choice_applies_to_every_page_not_just_one(client: TestClient):
    client.get("/language/sv?next=/")
    for path, expected in [
        ("/publications", "Publikationer"),
        ("/jobs", "Jobb"),
        ("/settings", "Inställningar"),
        ("/library", "Bibliotek"),
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert '<html lang="sv">' in resp.text, path
        assert expected in resp.text, path


def test_get_language_reads_session_directly(client: TestClient):
    # No session cookie yet -> default.
    from starlette.requests import Request

    scope = {
        "type": "http",
        "session": {},
    }
    request = Request(scope)
    assert get_language(request) == "en"

    request.scope["session"] = {"lang": "sv"}
    assert get_language(request) == "sv"
