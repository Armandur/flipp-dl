"""Tests for the OPDS catalog feeds in ``flipp_dl.web.opds`` (TASK-1294)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session
from flipp_dl.models import Issue, Publication
from flipp_dl.web.app import create_app

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)
    db_path = tmp_path / "flipp.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    app = create_app(db_path=db_path, output_root=output_root)

    pdf_path = output_root / "ka01.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    pub = Publication(
        custom_code="KA",
        name="Kalle Anka",
        next_issue_date="2026-01-01",
        issues=[
            Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01"),
            Issue(custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01"),
        ],
    )
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(pub)
        repo.set_watched("KA", True)
        db_issue1, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
        repo.upsert_issue(pub.issues[1], db_pub.id)
        repo.mark_issue_done(db_issue1.id, str(pdf_path))
        publication_id = db_pub.id

    # A second issue marked DONE but whose file has been removed from
    # disk - must never appear as an acquisition link (see opds.py).
    stale_issue = Issue(custom_code="ka03", issue_name="Nr 3", issue_date="2024-03-01")
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_issue3, _ = repo.upsert_issue(stale_issue, publication_id)
        repo.mark_issue_done(db_issue3.id, str(output_root / "missing.pdf"))

    # Unwatched publication - must not appear in the root feed.
    unwatched_pub = Publication(custom_code="OP", name="Other Pub", issues=[])
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(unwatched_pub)

    return TestClient(app)


@pytest.fixture()
def auth_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("FLIPP_PASSWORD", "s3cret")
    db_path = tmp_path / "flipp_auth.db"
    output_root = tmp_path / "output_auth"
    output_root.mkdir()
    app = create_app(db_path=db_path, output_root=output_root)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/opds - OPDS 1.2 Atom root/navigation feed
# ---------------------------------------------------------------------------


def test_opds_root_atom_content_type(client: TestClient):
    resp = client.get("/api/opds")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/atom+xml;profile=opds-catalog;kind=navigation"
    )


def test_opds_root_atom_structure(client: TestClient):
    resp = client.get("/api/opds")
    root = ET.fromstring(resp.content)
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    assert root.find("a:title", _ATOM_NS).text == "flipp-dl - bevakade publikationer"
    assert root.find("a:updated", _ATOM_NS) is not None

    self_links = [
        link for link in root.findall("a:link", _ATOM_NS) if link.get("rel") == "self"
    ]
    assert len(self_links) == 1
    assert self_links[0].get("href").endswith("/api/opds")

    entries = root.findall("a:entry", _ATOM_NS)
    # Only the watched publication (KA) shows up, not the unwatched OP.
    assert len(entries) == 1
    entry = entries[0]
    assert entry.find("a:title", _ATOM_NS).text == "Kalle Anka"

    sub_links = [
        link
        for link in entry.findall("a:link", _ATOM_NS)
        if link.get("rel") == "subsection"
    ]
    assert len(sub_links) == 1
    assert sub_links[0].get("href").endswith("/api/opds/KA")
    assert sub_links[0].get("type") == (
        "application/atom+xml;profile=opds-catalog;kind=acquisition"
    )


# ---------------------------------------------------------------------------
# GET /api/opds2 - OPDS 2.0 JSON root/navigation feed
# ---------------------------------------------------------------------------


def test_opds_root_json_content_type(client: TestClient):
    resp = client.get("/api/opds2")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/opds+json"


def test_opds_root_json_structure(client: TestClient):
    resp = client.get("/api/opds2")
    body = json.loads(resp.content)
    assert body["metadata"]["title"] == "flipp-dl - bevakade publikationer"
    assert any(link["rel"] == "self" for link in body["links"])

    assert len(body["navigation"]) == 1
    nav_entry = body["navigation"][0]
    assert nav_entry["title"] == "Kalle Anka"
    assert nav_entry["href"].endswith("/api/opds2/KA")
    assert nav_entry["type"] == "application/opds+json"


# ---------------------------------------------------------------------------
# GET /api/opds/{code} - OPDS 1.2 Atom acquisition feed
# ---------------------------------------------------------------------------


def test_opds_publication_atom_lists_only_downloaded_with_live_files(
    client: TestClient,
):
    resp = client.get("/api/opds/KA")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/atom+xml;profile=opds-catalog;kind=acquisition"
    )
    root = ET.fromstring(resp.content)
    entries = root.findall("a:entry", _ATOM_NS)
    # ka01 is DONE with a live file; ka02 is still NEW; ka03 is DONE but
    # its file is missing from disk - only ka01 shows up.
    assert len(entries) == 1
    entry = entries[0]
    assert entry.find("a:title", _ATOM_NS).text == "Nr 1"

    acq_links = [
        link
        for link in entry.findall("a:link", _ATOM_NS)
        if link.get("rel") == "http://opds-spec.org/acquisition"
    ]
    assert len(acq_links) == 1
    assert acq_links[0].get("href").endswith("/publications/KA/issues/ka01/file")
    assert acq_links[0].get("type") == "application/pdf"


def test_opds_publication_atom_404_for_unknown_publication(client: TestClient):
    resp = client.get("/api/opds/finns-inte")
    assert resp.status_code == 404


def test_opds_publication_file_link_is_fetchable(client: TestClient):
    """The link the feed advertises actually serves the PDF bytes."""
    resp = client.get("/api/opds/KA")
    root = ET.fromstring(resp.content)
    href = root.find(
        ".//a:entry/a:link[@rel='http://opds-spec.org/acquisition']", _ATOM_NS
    ).get("href")
    path = href.split("://", 1)[1].split("/", 1)[1]
    file_resp = client.get(f"/{path}")
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"] == "application/pdf"


# ---------------------------------------------------------------------------
# GET /api/opds2/{code} - OPDS 2.0 JSON acquisition feed
# ---------------------------------------------------------------------------


def test_opds_publication_json_structure(client: TestClient):
    resp = client.get("/api/opds2/KA")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/opds+json"
    body = json.loads(resp.content)
    assert body["metadata"]["title"] == "Kalle Anka"
    assert len(body["publications"]) == 1
    publication = body["publications"][0]
    assert publication["metadata"]["title"] == "Nr 1"
    assert publication["links"][0]["type"] == "application/pdf"
    assert publication["links"][0]["href"].endswith("/publications/KA/issues/ka01/file")


def test_opds_publication_json_404_for_unknown_publication(client: TestClient):
    resp = client.get("/api/opds2/finns-inte")
    assert resp.status_code == 404
    assert resp.json()["error"]


# ---------------------------------------------------------------------------
# Auth - same mechanism as the rest of /api/, see opds.py's docstring
# ---------------------------------------------------------------------------


def test_opds_feeds_require_login_when_auth_enabled(auth_client: TestClient):
    """An OPDS client can't fill in a login form, so it gets a JSON 401
    (same behaviour as every other /api/ route) rather than an HTML
    redirect it has no way to follow."""
    for path in ("/api/opds", "/api/opds2", "/api/opds/KA", "/api/opds2/KA"):
        resp = auth_client.get(path, follow_redirects=False)
        assert resp.status_code == 401, path
        assert resp.json()["error"] == "authentication required"


def test_opds_root_atom_reachable_after_login(auth_client: TestClient):
    import re

    login_page = auth_client.get("/login")
    csrf_token = re.search(
        r'name="_csrf_token" value="([^"]+)"', login_page.text
    ).group(1)
    login = auth_client.post(
        "/login",
        data={"password": "s3cret", "_csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert login.status_code in (302, 303)
    resp = auth_client.get("/api/opds")
    assert resp.status_code == 200


def test_opds_accepts_http_basic_credentials(tmp_path, monkeypatch):
    """OPDS readers can send Basic auth but cannot use the login form."""
    import base64

    from fastapi.testclient import TestClient

    from flipp_dl.web.app import create_app

    monkeypatch.setenv("FLIPP_PASSWORD", "hemligt")
    app = create_app(db_path=tmp_path / "flipp.db", output_root=tmp_path / "out")
    with TestClient(app) as client:
        assert client.get("/api/opds", follow_redirects=False).status_code == 401

        token = base64.b64encode(b"reader:hemligt").decode()
        ok = client.get("/api/opds", headers={"Authorization": f"Basic {token}"})
        assert ok.status_code == 200

        wrong = base64.b64encode(b"reader:fel").decode()
        assert (
            client.get(
                "/api/opds", headers={"Authorization": f"Basic {wrong}"}
            ).status_code
            == 401
        )
