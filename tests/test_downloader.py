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


def test_second_issue_with_the_same_name_gets_its_own_file(wired):
    """The colliding issue must not adopt the first issue's file."""
    factory, out = wired
    twin = Issue(custom_code="ka01-twin", issue_name="Nr 1", issue_date="2024-01-01")

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_issue(twin, repo.get_publication("KA").id)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        first = IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=False)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        second = IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, twin, skip_existing=True)

    assert first != second, "the twin reused the first issue's file"
    assert first.is_file() and second.is_file()
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        pub_id = repo.get_publication("KA").id
        a = repo.get_issue_by_code("ka01", pub_id)
        b = repo.get_issue_by_code("ka01-twin", pub_id)
        assert a.file_path != b.file_path
        assert repo.list_issues_sharing_files() == []


def test_download_refuses_target_outside_publication_folder(tmp_path, monkeypatch):
    client = FakeClient(pages=1)
    output_root = tmp_path / "out"
    outside = tmp_path / "outside.pdf"
    monkeypatch.setattr(
        "flipp_dl.downloader.issue_path", lambda *_args, **_kwargs: outside
    )

    downloader = IssueDownloader(client, output_root, workers=1)

    with pytest.raises(ValueError, match="escapes"):
        downloader.download_issue(PUB, ISSUE, skip_existing=False)

    assert not outside.exists()


def test_skipping_an_existing_file_still_marks_the_issue_done(wired):
    """A re-run over an existing file must not leave the row queued."""
    factory, out = wired

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=False)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        repo.mark_issue_queued(issue_id)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=True)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).status == "done"


def test_download_issue_records_file_size(wired):
    """The size-estimate feature (TASK-1362) reads this column - never disk."""
    factory, out = wired

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        target = IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=False)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        assert repo.get_issue(issue_id).file_size == target.stat().st_size


def test_skipping_an_existing_file_still_records_its_size(wired):
    """Skip-existing marks the row done straight from what's already on disk."""
    factory, out = wired

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        target = IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=False)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        repo.mark_issue_queued(issue_id)
        repo.get_issue(issue_id).file_size = None

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        IssueDownloader(
            FakeClient(pages=2), out, workers=1, repository=repo
        ).download_issue(PUB, ISSUE, skip_existing=True)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).file_size == target.stat().st_size


def test_release_duplicate_file_claims_keeps_the_first_download(wired):
    """Recovery leaves the real owner alone and frees the other."""
    factory, _out = wired
    from datetime import datetime

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        pub_id = repo.get_publication("KA").id
        twin = Issue(
            custom_code="ka01-twin", issue_name="Nr 1", issue_date="2024-01-01"
        )
        db_twin, _ = repo.upsert_issue(twin, pub_id)
        original = repo.get_issue_by_code("ka01", pub_id)
        shared = "/output/Kalle Anka/dupe.pdf"
        repo.mark_issue_done(original.id, shared)
        repo.mark_issue_done(db_twin.id, shared)
        original.downloaded_at = datetime(2024, 1, 1)
        db_twin.downloaded_at = datetime(2024, 6, 1)
        original_id, twin_id = original.id, db_twin.id

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        assert repo.release_duplicate_file_claims() == 1

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(original_id).status == "done"
        assert repo.get_issue(original_id).file_path == "/output/Kalle Anka/dupe.pdf"
        assert repo.get_issue(twin_id).status == "new"
        assert repo.get_issue(twin_id).file_path is None


# ---------------------------------------------------------------------------
# Preview (TASK-1344)
# ---------------------------------------------------------------------------


def test_preview_writes_outside_output_root_and_leaves_status_untouched(
    wired, tmp_path
):
    """A preview must not look like a real download in any way."""
    factory, out = wired
    client = FakeClient(pages=5)
    preview_root = tmp_path / "previews"

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        downloader = IssueDownloader(client, out, workers=1, repository=repo)
        preview_path = downloader.preview_issue(PUB, ISSUE, preview_root=preview_root)

        assert preview_path.is_file()
        assert preview_root in preview_path.parents
        # Not anywhere under output_root, however it's spelled.
        assert out.resolve() not in preview_path.resolve().parents

        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        assert issue.status == "new"
        assert issue.file_path is None


def test_preview_fetches_only_the_requested_page_count(wired, tmp_path):
    """A preview is cheap: it must not download every page."""
    factory, out = wired
    client = FakeClient(pages=40)
    fetched: list[str] = []
    real_download = client.download_pdf

    def spy(url: str) -> bytes:
        fetched.append(url)
        return real_download(url)

    client.download_pdf = spy
    preview_root = tmp_path / "previews"

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        IssueDownloader(client, out, workers=1, repository=repo).preview_issue(
            PUB, ISSUE, pages=3, preview_root=preview_root
        )

    assert len(fetched) == 3


def test_preview_does_not_go_through_target_path_or_skip_existing(wired, tmp_path):
    """preview_issue must not reuse download_issue's real-file machinery."""
    factory, out = wired
    client = FakeClient(pages=2)
    preview_root = tmp_path / "previews"

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        downloader = IssueDownloader(client, out, workers=1, repository=repo)

        def boom(*_args, **_kwargs):
            raise AssertionError("preview_issue must not call _target_path")

        downloader._target_path = boom  # type: ignore[method-assign]
        preview_path = downloader.preview_issue(PUB, ISSUE, preview_root=preview_root)

    assert preview_path.is_file()


def test_purge_old_previews_removes_only_stale_files(tmp_path):
    import os
    import time

    from flipp_dl.downloader import purge_old_previews

    root = tmp_path / "previews"
    root.mkdir()
    stale = root / "preview-old-aaaa1111.pdf"
    fresh = root / "preview-new-bbbb2222.pdf"
    stale.write_bytes(b"%PDF-old")
    fresh.write_bytes(b"%PDF-new")

    old_time = time.time() - 3600
    os.utime(stale, (old_time, old_time))

    removed = purge_old_previews(root, max_age_seconds=600)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_purge_old_previews_on_missing_dir_is_a_noop(tmp_path):
    from flipp_dl.downloader import purge_old_previews

    assert purge_old_previews(tmp_path / "does-not-exist") == 0


def test_download_issue_marks_error_when_the_page_list_fetch_fails(wired):
    """TASK-1377: a failure before any page is fetched must still land as error.

    fetch_issue_pdf_urls used to run outside download_issue's own
    try/except, so a failure there left the issue in whatever status it
    already had instead of being marked error - the scheduler's own
    except-block papered over this for queue-driven runs, but a direct
    caller (e.g. the CLI) saw the incomplete behaviour.
    """
    factory, out = wired

    class BoomingClient(FakeClient):
        def fetch_issue_pdf_urls(self, _pub_code: str, _issue_code: str) -> list[str]:
            raise RuntimeError("token invalid")

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        downloader = IssueDownloader(
            BoomingClient(pages=2), out, workers=1, repository=repo
        )
        with pytest.raises(RuntimeError, match="token invalid"):
            downloader.download_issue(PUB, ISSUE, skip_existing=False)

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        assert issue.status == "error"


def test_download_without_destination_uses_primary_root(tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"

    target = IssueDownloader(
        FakeClient(pages=1),
        primary,
        secondary_output_root=secondary,
        workers=1,
    ).download_issue(PUB, ISSUE, skip_existing=False)

    assert target.is_relative_to(primary)
    assert target.is_file()


def test_download_with_secondary_destination_uses_secondary_root(tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    publication = Publication(custom_code="FA", name="Fantomen")
    publication.destination = "secondary"

    target = IssueDownloader(
        FakeClient(pages=1),
        primary,
        secondary_output_root=secondary,
        workers=1,
    ).download_issue(publication, ISSUE, skip_existing=False)

    assert target.is_relative_to(secondary)
    assert target.is_file()


def test_download_with_missing_secondary_root_falls_back_to_primary(tmp_path):
    primary = tmp_path / "primary"
    publication = Publication(custom_code="FA", name="Fantomen")
    publication.destination = "secondary"

    target = IssueDownloader(FakeClient(pages=1), primary, workers=1).download_issue(
        publication, ISSUE, skip_existing=False
    )

    assert target.is_relative_to(primary)
    assert target.is_file()
