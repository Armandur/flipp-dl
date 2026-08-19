"""Tests for the JSON API endpoints in ``flipp_dl.web.api_routes``."""

from __future__ import annotations

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
    db_path = tmp_path / "flipp.db"
    output_root = tmp_path / "output"
    output_root.mkdir()
    app = create_app(db_path=db_path, output_root=output_root)

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
        repo.mark_issue_done(db_issue1.id, str(output_root / "ka01.pdf"))
        issue1_id = db_issue1.id

    unwatched_pub = Publication(custom_code="OP", name="Other Pub", issues=[])
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(unwatched_pub)

    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.create_job("poll", {"code": "KA"})
        job2 = repo.create_job("download", {"issue_id": issue1_id})
        repo.start_job(job2.id)
        repo.finish_job(job2.id)

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
# GET /api/publications
# ---------------------------------------------------------------------------


def test_api_publications_list(client: TestClient):
    resp = client.get("/api/publications")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert len(body) == 2
    ka = next(p for p in body if p["custom_code"] == "KA")
    assert ka == {
        "custom_code": "KA",
        "name": "Kalle Anka",
        "watched": True,
        "num_issues": 2,
        "num_downloaded": 1,
        "next_issue_date": "2026-01-01",
    }
    op = next(p for p in body if p["custom_code"] == "OP")
    assert op["watched"] is False
    assert op["num_issues"] == 0


# ---------------------------------------------------------------------------
# GET /api/publications/{code}
# ---------------------------------------------------------------------------


def test_api_publication_detail(client: TestClient):
    resp = client.get("/api/publications/KA")
    assert resp.status_code == 200
    body = resp.json()
    assert body["custom_code"] == "KA"
    assert body["name"] == "Kalle Anka"
    assert len(body["issues"]) == 2
    # Newest issue_date first
    assert body["issues"][0]["custom_code"] == "ka02"
    done = next(i for i in body["issues"] if i["custom_code"] == "ka01")
    assert done["status"] == "done"
    assert done["downloaded_at"] is not None


def test_api_publication_detail_404(client: TestClient):
    resp = client.get("/api/publications/finns-inte")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


def test_api_jobs_list(client: TestClient):
    resp = client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    statuses = {j["status"] for j in body}
    assert statuses == {"queued", "done"}
    done_job = next(j for j in body if j["status"] == "done")
    assert done_job["job_type"] == "download"
    assert done_job["finished_at"] is not None


def test_api_jobs_list_status_filter(client: TestClient):
    resp = client.get("/api/jobs", params={"status": "done"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "done"


def test_api_jobs_list_unknown_status_is_ignored(client: TestClient):
    resp = client.get("/api/jobs", params={"status": "bogus"})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_api_publications_requires_login_when_auth_enabled(auth_client: TestClient):
    """API clients get a 401 they can act on, not a redirect to HTML."""
    resp = auth_client.get("/api/publications", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["error"] == "authentication required"


def test_api_publications_reachable_after_login(auth_client: TestClient):
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
    resp = auth_client.get("/api/publications")
    assert resp.status_code == 200
    assert resp.json() == []


def test_api_returns_401_json_not_a_login_redirect(tmp_path, monkeypatch):
    """A script can't fill in a login form, so it needs a real status."""
    monkeypatch.setenv("FLIPP_PASSWORD", "hemligt")
    app = create_app(db_path=tmp_path / "flipp.db", output_root=tmp_path / "out")
    with TestClient(app) as client:
        resp = client.get("/api/publications", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.json()["error"] == "authentication required"
        # HTML pages keep redirecting to the form.
        assert client.get("/", follow_redirects=False).status_code == 302
