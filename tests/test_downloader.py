"""Tests for progress writes during a download (TASK-1340).

A busy SQLite file used to fail the whole download: the per-page
progress write raised ``database is locked``, which poisoned the
session so every later write - including ``mark_issue_done`` - failed
too, and a finished PDF was recorded as an error.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy.exc import OperationalError

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session, make_session_factory
from flipp_dl.downloader import IssueDownloader
from flipp_dl.models import Issue, Publication


def _one_page_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class FakeClient:
    """Minimal FlippClient stand-in returning the same one-page PDF."""

    def __init__(self, pages: int) -> None:
        self.pdf_urls = [f"http://example.invalid/page{i}.pdf" for i in range(pages)]
        self.page = _one_page_pdf()

    def fetch_issue_pdf_urls(self, _pub_code: str, _issue_code: str) -> list[str]:
        return list(self.pdf_urls)

    def download_pdf(self, _url: str) -> bytes:
        return self.page


PUB = Publication(custom_code="KA", name="Kalle Anka")
ISSUE = Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01")


@pytest.fixture()
def wired(tmp_path: Path):
    """Return (downloader-factory, repo-session) with the issue seeded."""
    factory = make_session_factory(tmp_path / "flipp.db")
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(PUB)
        repo.upsert_issue(ISSUE, db_pub.id)
    return factory, tmp_path / "out"


def test_progress_writes_are_throttled_but_always_reach_the_total(wired, monkeypatch):
    """40 pages must not mean 40 commits - but the last one must land."""
    factory, out = wired
    client = FakeClient(pages=40)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        writes: list[tuple[int, int]] = []
        real_update = repo.update_issue_progress

        def spy(issue_id: int, current: int, total: int) -> None:
            writes.append((current, total))
            real_update(issue_id, current, total)

        monkeypatch.setattr(repo, "update_issue_progress", spy)
        # Workers=1 keeps page completion deterministic for the test.
        IssueDownloader(client, out, workers=1, repository=repo).download_issue(
            PUB, ISSUE, skip_existing=False
        )

    # The 0/40 priming write plus the final 40/40 write; the pages in
    # between finish well inside one second and are skipped.
    assert writes[0] == (0, 40)
    assert writes[-1] == (40, 40)
    assert len(writes) < 40, f"expected throttled writes, got {len(writes)}"


def test_locked_database_during_progress_does_not_fail_the_download(wired, monkeypatch):
    """A locked DB on a progress write must not fail a good download."""
    factory, out = wired
    client = FakeClient(pages=3)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        calls = {"n": 0}
        real_update = repo.update_issue_progress

        def flaky(issue_id: int, current: int, total: int) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError(
                    "UPDATE issues", {}, Exception("database is locked")
                )
            real_update(issue_id, current, total)

        monkeypatch.setattr(repo, "update_issue_progress", flaky)
        target = IssueDownloader(
            client, out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=False)

    assert target.is_file()
    # The session survived, so the issue was still marked done.
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        assert issue.status == "done"
        assert issue.file_path == str(target)


def test_connection_waits_for_a_busy_writer(tmp_path: Path):
    """busy_timeout must be set, otherwise SQLite raises immediately."""
    factory = make_session_factory(tmp_path / "flipp.db")
    with get_session(factory) as session:
        timeout = session.execute(
            __import__("sqlalchemy").text("PRAGMA busy_timeout")
        ).scalar()
    assert timeout >= 5000
