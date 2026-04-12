"""Tests for the FastAPI routes that touch the filesystem.

These tests focus on the path-traversal guard (``_safe_output_file``) and
the endpoints that stream files from disk – both for the per-issue PDF
viewer and the ``/library`` file browser. The scheduler is not started
here; we create the app via :func:`create_app` and hit the routes
through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session
from flipp_dl.models import Issue, Publication
from flipp_dl.web.app import create_app
from flipp_dl.web.routes import _safe_output_file

# ---------------------------------------------------------------------------
# _safe_output_file – unit tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def output_tree(tmp_path: Path) -> Path:
    """Build an output directory with a couple of PDFs for the tests."""
    root = tmp_path / "output"
    (root / "Kalle Anka").mkdir(parents=True)
    (root / "Kalle Anka" / "ka01.pdf").write_bytes(b"%PDF-1.4\n%dummy\n")
    (root / "Kalle Anka" / "ka02.pdf").write_bytes(b"%PDF-1.4\n%another\n")
    (root / "README.txt").write_text("not a pdf")

    # A decoy file OUTSIDE the output root to verify traversal guards.
    (tmp_path / "secrets.pdf").write_bytes(b"%PDF-1.4\n%secret\n")
    return root


def test_safe_output_file_resolves_relative(output_tree: Path):
    resolved = _safe_output_file(output_tree, "Kalle Anka/ka01.pdf")
    assert resolved is not None
    assert resolved == (output_tree / "Kalle Anka" / "ka01.pdf").resolve()


def test_safe_output_file_resolves_absolute(output_tree: Path):
    abs_path = str((output_tree / "Kalle Anka" / "ka01.pdf").resolve())
    resolved = _safe_output_file(output_tree, abs_path)
    assert resolved is not None


def test_safe_output_file_rejects_missing(output_tree: Path):
    assert _safe_output_file(output_tree, "Kalle Anka/ghost.pdf") is None


def test_safe_output_file_rejects_dot_dot_traversal(output_tree: Path):
    # ``../secrets.pdf`` resolves outside output_root
    assert _safe_output_file(output_tree, "../secrets.pdf") is None


def test_safe_output_file_rejects_absolute_outside(output_tree: Path):
    sibling = (output_tree.parent / "secrets.pdf").resolve()
    assert sibling.is_file()  # sanity check
    assert _safe_output_file(output_tree, str(sibling)) is None


def test_safe_output_file_rejects_none_and_empty(output_tree: Path):
    assert _safe_output_file(output_tree, None) is None
    assert _safe_output_file(output_tree, "") is None


def test_safe_output_file_rejects_directory(output_tree: Path):
    # ``Kalle Anka`` exists but is a directory, not a file.
    assert _safe_output_file(output_tree, "Kalle Anka") is None


# ---------------------------------------------------------------------------
# End-to-end route tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, output_tree: Path, monkeypatch) -> TestClient:
    """Spin up a fresh FastAPI app wired to a temp DB and output dir."""
    # Make sure no leftover FLIPP_PASSWORD forces auth on us.
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)

    db_path = tmp_path / "flipp.db"
    app = create_app(db_path=db_path, output_root=output_tree)

    # Seed a watched publication with one downloaded issue pointing at the
    # real file under output_tree so the file-serving route has something
    # to find.
    pub = Publication(
        custom_code="KA",
        name="Kalle Anka",
        issues=[Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01")],
    )
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(pub)
        db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
        repo.mark_issue_done(db_issue.id, str(output_tree / "Kalle Anka" / "ka01.pdf"))

    # Also seed a "done" row whose file has since vanished, so we can
    # verify the route returns 404 rather than a stale FileResponse.
    ghost_pub = Publication(custom_code="GH", name="Ghost", issues=[])
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(ghost_pub)
        ghost_issue = Issue(
            custom_code="gh01", issue_name="Nr 1", issue_date="2024-02-01"
        )
        db_issue, _ = repo.upsert_issue(ghost_issue, db_pub.id)
        repo.mark_issue_done(db_issue.id, str(output_tree / "Ghost" / "missing.pdf"))

    return TestClient(app)


def test_serve_issue_file_returns_pdf(client: TestClient):
    resp = client.get("/publications/KA/issues/ka01/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    # Browsers get ``inline`` so they render instead of downloading.
    assert "inline" in resp.headers.get("content-disposition", "").lower()
    assert resp.content.startswith(b"%PDF")


def test_serve_issue_file_404_for_missing_file(client: TestClient):
    resp = client.get("/publications/GH/issues/gh01/file")
    assert resp.status_code == 404


def test_serve_issue_file_404_for_unknown_publication(client: TestClient):
    resp = client.get("/publications/NOPE/issues/ka01/file")
    assert resp.status_code == 404


def test_library_index_lists_pdfs(client: TestClient):
    resp = client.get("/library")
    assert resp.status_code == 200
    body = resp.text
    assert "ka01.pdf" in body
    assert "ka02.pdf" in body
    # README.txt is not a PDF and must be filtered out.
    assert "README.txt" not in body


def test_library_file_serves_existing_pdf(client: TestClient):
    resp = client.get("/library/file/Kalle Anka/ka01.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_library_file_rejects_traversal(client: TestClient):
    # The FastAPI path converter collapses some ``..`` segments before
    # reaching the handler, but any request that escapes the output root
    # must come back as 404 – never a FileResponse for the sibling file.
    resp = client.get("/library/file/../secrets.pdf")
    assert resp.status_code == 404


def test_library_file_rejects_missing(client: TestClient):
    resp = client.get("/library/file/does-not-exist.pdf")
    assert resp.status_code == 404
