"""Tests for the catalogue/backup routes on the settings page (TASK-1444).

The web counterpart to ``--import-catalog``, ``--export-codes`` and
``--import-backup``. These hit the routes, not just the shared helpers in
:mod:`flipp_dl.codes`, so the CSRF check, the multipart upload and the
download headers are covered too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session
from flipp_dl.models import Issue, Publication
from flipp_dl.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)
    monkeypatch.setenv("FLIPP_COVER_CACHE", str(tmp_path / "covers"))
    output_root = tmp_path / "output"
    output_root.mkdir()
    app = create_app(db_path=tmp_path / "flipp.db", output_root=output_root)
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(
            Publication(custom_code="KA", name="Kalle Anka")
        )
        repo.upsert_issue(
            Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01"),
            db_pub.id,
        )
    return TestClient(app)


def _csrf(client: TestClient) -> str:
    page = client.get("/settings")
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page.text)
    assert match, "no CSRF token rendered on the settings page"
    return match.group(1)


# ---------------------------------------------------------------------------
# Export - a download, not an HTMX swap
# ---------------------------------------------------------------------------


def test_export_codes_downloads_the_backup(client: TestClient):
    response = client.get("/settings/export-codes")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert ".json" in response.headers["content-disposition"]
    payload = response.json()
    assert [p["customPublicationCode"] for p in payload["publications"]] == ["KA"]
    assert [i["issue_code"] for i in payload["issues"]] == ["ka01"]


# ---------------------------------------------------------------------------
# Uploads - CSRF, then the happy paths
# ---------------------------------------------------------------------------


def test_upload_without_csrf_token_is_rejected(client: TestClient):
    _csrf(client)  # establish the session so only the token value is missing
    response = client.post(
        "/settings/import-backup",
        files={"backup": ("backup.json", b'{"publications": []}', "application/json")},
    )
    assert response.status_code == 400


def test_upload_with_wrong_csrf_token_is_rejected(client: TestClient):
    _csrf(client)
    response = client.post(
        "/settings/import-backup",
        data={"_csrf_token": "nope"},
        files={"backup": ("backup.json", b'{"publications": []}', "application/json")},
    )
    assert response.status_code == 400


def test_import_backup_restores_publications_and_issue_codes(client: TestClient):
    token = _csrf(client)
    payload = {
        "publications": [
            {
                "customPublicationCode": "BI",
                "publicationCode": "SE-CAR",
                "name": "Bilar",
                "delisted": False,
            },
            {
                "customPublicationCode": "BA",
                "publicationCode": None,
                "name": "Båtnytt",
                "delisted": True,
            },
        ],
        "issues": [
            {
                "issue_code": "b1",
                "publication_code": "BI",
                "issue_name": "Nr 1",
                "issue_date": "2024-03-01",
            },
            {
                "issue_code": "x1",
                "publication_code": "GONE",
                "issue_name": "Nr 9",
                "issue_date": "2024-03-01",
            },
        ],
    }
    response = client.post(
        "/settings/import-backup",
        data={"_csrf_token": token},
        files={
            "backup": (
                "backup.json",
                json.dumps(payload).encode("utf-8"),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    assert "2" in response.text  # restored publications
    assert "GONE" in response.text  # the issue whose publication is unknown
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        bilar = repo.get_publication("BI")
        assert bilar is not None
        assert bilar.publication_code == "SE-CAR"
        assert {i.custom_code for i in repo.list_issues(publication_id=bilar.id)} == {
            "b1"
        }
        assert repo.get_publication("BA").delisted_at is not None


def test_import_catalog_adds_publications_and_the_unlisted_companion(
    client: TestClient,
):
    token = _csrf(client)
    catalog = [
        {"customPublicationCode": "BI", "publicationCode": "SE-CAR", "name": "Bilar"},
        {
            "customPublicationCode": "KA",
            "publicationCode": "SE-KA",
            "name": "Kalle Anka",
        },
    ]
    unlisted = [{"customPublicationCode": "HE", "name": "Hemligt"}]

    response = client.post(
        "/settings/import-catalog",
        data={"_csrf_token": token},
        files={
            "catalog": (
                "alla.json",
                json.dumps(catalog).encode("utf-8"),
                "application/json",
            ),
            "unlisted": (
                "olistade.json",
                json.dumps(unlisted).encode("utf-8"),
                "application/json",
            ),
        },
    )

    assert response.status_code == 200
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_publication("BI") is not None
        assert repo.get_publication("KA").publication_code == "SE-KA"
        hemligt = repo.get_publication("HE")
        assert hemligt is not None and hemligt.delisted_at is not None


def test_import_catalog_works_without_the_unlisted_file(client: TestClient):
    token = _csrf(client)
    response = client.post(
        "/settings/import-catalog",
        data={"_csrf_token": token},
        files={
            "catalog": (
                "alla.json",
                json.dumps([{"customPublicationCode": "BI", "name": "Bilar"}]).encode(
                    "utf-8"
                ),
                "application/json",
            )
        },
    )

    assert response.status_code == 200
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("BI") is not None


# ---------------------------------------------------------------------------
# Bad files get a readable message, not a stack trace
# ---------------------------------------------------------------------------


def test_a_file_that_is_not_json_is_reported(client: TestClient):
    token = _csrf(client)
    response = client.post(
        "/settings/import-backup",
        data={"_csrf_token": token},
        files={"backup": ("backup.json", b"not json at all", "application/json")},
    )
    assert response.status_code == 200
    assert "not valid JSON" in response.text


def test_a_catalogue_of_the_wrong_shape_is_reported(client: TestClient):
    token = _csrf(client)
    response = client.post(
        "/settings/import-catalog",
        data={"_csrf_token": token},
        files={"catalog": ("alla.json", b'{"nope": 1}', "application/json")},
    )
    assert response.status_code == 200
    assert "catalogue file" in response.text


def test_no_file_selected_is_reported(client: TestClient):
    token = _csrf(client)
    response = client.post("/settings/import-backup", data={"_csrf_token": token})
    assert response.status_code == 200
    assert "No file was selected" in response.text


def test_export_then_import_round_trips_through_the_web(client: TestClient, tmp_path):
    """Export from one instance, restore into an empty one - codes survive."""
    exported = client.get("/settings/export-codes").content

    other = create_app(db_path=tmp_path / "other.db", output_root=tmp_path / "output")
    other_client = TestClient(other)
    token_page = other_client.get("/settings")
    token = re.search(r'name="_csrf_token" value="([^"]+)"', token_page.text).group(1)
    response = other_client.post(
        "/settings/import-backup",
        data={"_csrf_token": token},
        files={"backup": ("backup.json", exported, "application/json")},
    )

    assert response.status_code == 200
    with get_session(other.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        assert pub is not None and pub.name == "Kalle Anka"
        assert {i.custom_code for i in repo.list_issues(publication_id=pub.id)} == {
            "ka01"
        }
