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


def test_issue_row_partial_returns_row(client: TestClient):
    # The row endpoint returns just the <tr>; the caller is HTMX swapping
    # a single row out so a full HTML document would break the swap.
    resp = client.get("/publications/KA/issues/ka01/row")
    assert resp.status_code == 200
    assert "<tr" in resp.text
    assert "issue-row-ka01" in resp.text


def test_issue_row_partial_shows_progress_while_downloading(
    client: TestClient, tmp_path: Path
):
    # Put the issue into DOWNLOADING with 3/10 progress and verify the
    # polling partial renders the counter so the user sees live updates.
    app = client.app
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        repo.mark_issue_downloading(issue.id)
        repo.update_issue_progress(issue.id, 3, 10)

    resp = client.get("/publications/KA/issues/ka01/row")
    assert resp.status_code == 200
    assert "Downloading 3/10" in resp.text
    # Polling attributes must be present so HTMX keeps refreshing.
    assert "hx-trigger" in resp.text
    assert "every 2s" in resp.text


# ---------------------------------------------------------------------------
# Jobs view – status filter and queue counters (TASK-1330)
# ---------------------------------------------------------------------------


def _seed_jobs(client: TestClient, queued: int, done: int) -> None:
    """Create *done* finished jobs and *queued* queued ones.

    The queued jobs are created FIRST so they end up oldest - that is
    the case that used to fall off the end of the jobs page.
    """
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        for i in range(queued):
            repo.create_job("download", {"issue_id": i})
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        for _ in range(done):
            job = repo.create_job("poll")
            repo.finish_job(job.id)


def test_jobs_page_filters_by_status_in_the_query(client: TestClient):
    """A queued job must stay visible behind a page-full of newer jobs.

    120 finished jobs is more than the 100-row page, so without a
    DB-level filter the queued job would be invisible on /jobs.
    """
    _seed_jobs(client, queued=1, done=120)

    unfiltered = client.get("/jobs")
    assert unfiltered.status_code == 200
    assert 'class="badge badge-queued"' not in unfiltered.text  # off the page

    filtered = client.get("/jobs?status=queued")
    assert filtered.status_code == 200
    assert 'class="badge badge-queued"' in filtered.text
    assert 'class="badge badge-done"' not in filtered.text


def test_jobs_page_counts_cover_the_whole_table(client: TestClient):
    """The counters must reflect every row, not just the listed page."""
    _seed_jobs(client, queued=3, done=120)

    resp = client.get("/jobs")
    assert resp.status_code == 200
    # Rendered as e.g. `queued<span class="count">3</span>`
    assert 'queued<span class="count">3</span>' in resp.text
    assert 'done<span class="count">120</span>' in resp.text


def test_jobs_page_ignores_unknown_status(client: TestClient):
    _seed_jobs(client, queued=2, done=1)

    resp = client.get("/jobs?status=bogus")
    assert resp.status_code == 200
    # Falls back to "all", so both statuses are listed.
    assert 'class="badge badge-queued"' in resp.text
    assert 'class="badge badge-done"' in resp.text


def test_dashboard_shows_queue_card_and_polls(client: TestClient):
    _seed_jobs(client, queued=4, done=2)

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/jobs?status=queued"' in resp.text
    assert 'hx-get="/stats/cards"' in resp.text


def test_stats_cards_partial_reflects_queue_depth(client: TestClient):
    _seed_jobs(client, queued=4, done=2)

    resp = client.get("/stats/cards")
    assert resp.status_code == 200
    assert "In Queue" in resp.text
    # The queue card shows the queued count.
    assert ">4</div>" in resp.text
    # The partial re-arms its own polling so the swap keeps refreshing.
    assert 'hx-trigger="every 5s"' in resp.text


# ---------------------------------------------------------------------------
# Job target column and detail view (TASK-1333)
# ---------------------------------------------------------------------------


def _seed_download_job(client: TestClient, payload: str) -> int:
    """Create one download job with a raw payload string, return its id."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        job = repo.create_job("download")
        job.payload = payload
        session.flush()
        return job.id


def test_jobs_list_names_the_issue_a_job_targets(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        issue_id = issue.id
    _seed_download_job(client, f'{{"issue_id": {issue_id}}}')

    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "Kalle Anka" in resp.text
    assert "Nr 1" in resp.text


def test_jobs_list_survives_broken_and_dangling_payloads(client: TestClient):
    """A job pointing nowhere must render as a blank target, not a 500."""
    _seed_download_job(client, "not json at all")
    _seed_download_job(client, '{"issue_id": 999999}')
    _seed_download_job(client, "{}")

    resp = client.get("/jobs")
    assert resp.status_code == 200


def test_job_detail_shows_payload_and_target(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        issue_id = issue.id
    job_id = _seed_download_job(client, f'{{"issue_id": {issue_id}}}')

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "Kalle Anka" in resp.text
    assert "issue_id" in resp.text  # payload renderas HTML-escapad
    assert 'href="/publications/KA"' in resp.text


def test_job_detail_handles_broken_payload(client: TestClient):
    job_id = _seed_download_job(client, "not json at all")

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "not json at all" in resp.text


def test_job_detail_returns_404_for_unknown_job(client: TestClient):
    assert client.get("/jobs/424242").status_code == 404


def test_jobs_list_links_a_downloaded_issue_to_its_file(client: TestClient):
    """When the target is on disk, the issue name links to the PDF."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
    _seed_download_job(client, f'{{"issue_id": {issue_id}}}')

    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "/publications/KA/issues/ka01/file" in resp.text


def test_jobs_list_does_not_link_a_missing_file(client: TestClient):
    """The ghost issue is marked done but its file is gone - no link."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue_id = repo.get_issue_by_code("gh01", repo.get_publication("GH").id).id
    _seed_download_job(client, f'{{"issue_id": {issue_id}}}')

    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "/publications/GH/issues/gh01/file" not in resp.text
    assert "Nr 1" in resp.text  # namnet visas ändå, bara inte som länk


def test_job_detail_links_a_downloaded_issue_to_its_file(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
    job_id = _seed_download_job(client, f'{{"issue_id": {issue_id}}}')

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    assert "/publications/KA/issues/ka01/file" in resp.text


def test_publications_list_shows_downloaded_count(client: TestClient):
    """The publications table reports downloaded vs total issues."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        # Second issue, not downloaded, so the ratio is 1/2 rather than 1/1.
        repo.upsert_issue(
            Issue(custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01"),
            pub.id,
        )

    resp = client.get("/publications")
    assert resp.status_code == 200
    assert "Downloaded" in resp.text
    assert "/2" in resp.text


# ---------------------------------------------------------------------------
# Timestamp rendering (TASK-1339)
# ---------------------------------------------------------------------------


def test_localtime_shifts_stored_utc_to_the_display_zone(monkeypatch):
    """Stored timestamps are naive UTC; the UI must show local time."""
    from datetime import datetime

    from flipp_dl.web.app import _localtime

    monkeypatch.setenv("FLIPP_TZ", "Europe/Stockholm")

    # Winter: CET is UTC+1
    assert _localtime(datetime(2026, 1, 15, 12, 0)) == "2026-01-15 13:00"
    # Summer: CEST is UTC+2 - the case that made the UI look two hours off
    assert _localtime(datetime(2026, 8, 19, 12, 0)) == "2026-08-19 14:00"


def test_localtime_honours_flipp_tz(monkeypatch):
    from datetime import datetime

    from flipp_dl.web.app import _localtime

    monkeypatch.setenv("FLIPP_TZ", "UTC")
    assert _localtime(datetime(2026, 8, 19, 12, 0)) == "2026-08-19 12:00"


def test_localtime_falls_back_on_unknown_zone(monkeypatch):
    from datetime import datetime

    from flipp_dl.web.app import _localtime

    monkeypatch.setenv("FLIPP_TZ", "Mars/Olympus_Mons")
    monkeypatch.delenv("TZ", raising=False)
    # Falls back to Europe/Stockholm rather than raising on every page.
    assert _localtime(datetime(2026, 8, 19, 12, 0)) == "2026-08-19 14:00"


def test_localtime_renders_missing_timestamp_as_dash():
    from flipp_dl.web.app import _localtime

    assert _localtime(None) == "—"


def test_jobs_page_renders_timestamps_in_local_time(client: TestClient, monkeypatch):
    """End-to-end: a job created now shows the local hour, not the UTC hour."""
    from datetime import datetime, timezone

    from zoneinfo import ZoneInfo

    monkeypatch.setenv("FLIPP_TZ", "Europe/Stockholm")
    job_id = _seed_download_job(client, "{}")
    with get_session(client.app.state.session_factory) as session:
        created = DownloadRepository(session).get_job(job_id).created_at

    expected = (
        created.replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Europe/Stockholm"))
        .strftime("%Y-%m-%d %H:%M")
    )
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert expected in resp.text


def test_library_file_times_are_not_shifted_twice(client: TestClient, monkeypatch):
    """File mtimes come from the filesystem, not from the UTC-storing DB.

    Reading them as naive local time and then converting as if they were
    UTC would show every file two hours into the future in summer.
    """
    from datetime import datetime

    from zoneinfo import ZoneInfo

    monkeypatch.setenv("FLIPP_TZ", "Europe/Stockholm")
    pdf = client.app.state.output_root / "Kalle Anka" / "ka01.pdf"
    expected = datetime.fromtimestamp(
        pdf.stat().st_mtime, tz=ZoneInfo("Europe/Stockholm")
    ).strftime("%Y-%m-%d %H:%M")

    resp = client.get("/library")
    assert resp.status_code == 200
    assert expected in resp.text
