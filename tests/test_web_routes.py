"""Tests for the FastAPI routes that touch the filesystem.

These tests focus on the path-traversal guard (``_safe_output_file``) and
the endpoints that stream files from disk – both for the per-issue PDF
viewer and the ``/library`` file browser. The scheduler is not started
here; we create the app via :func:`create_app` and hit the routes
through :class:`fastapi.testclient.TestClient`.
"""

from __future__ import annotations

import re
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


def test_publications_list_does_not_query_the_issues_table(client: TestClient):
    """TASK-1338: the list view must not load every issue row.

    ``list_publications()`` used to selectinload the whole ``issues``
    relationship for every publication just to compute two counters -
    17660 rows on the production instance for a page that renders two
    integers per row. Any SELECT that touches ``issues`` here (beyond the
    aggregated GROUP BY, which is an acceptable single query) is the bug
    coming back.
    """
    from sqlalchemy import event

    engine = client.app.state.session_factory.kw["bind"]
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/publications")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    select_statements = [
        s for s in statements if s.strip().upper().startswith("SELECT")
    ]
    assert select_statements, "expected at least one SELECT for /publications"

    issues_statements = [s for s in select_statements if "FROM issues" in s]
    assert issues_statements, "expected the aggregated issue-count query to run"
    for stmt in issues_statements:
        # The only query touching ``issues`` may be the aggregated
        # GROUP BY count/sum - anything else means a row-level SELECT
        # (custom_code, issue_name, file_path, ...) of the whole table
        # crept back in, which is exactly what this task removes.
        assert "GROUP BY" in stmt, stmt
        assert "issues.custom_code" not in stmt, stmt
        assert "issues.issue_name" not in stmt, stmt
        assert "issues.file_path" not in stmt, stmt
        assert "issues.discovered_at" not in stmt, stmt


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
    from datetime import timezone
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


# ---------------------------------------------------------------------------
# Cancelling a stuck issue (TASK-1341)
# ---------------------------------------------------------------------------


def _csrf_for(client: TestClient) -> str:
    """Fetch a page so the session cookie and CSRF token are established."""
    page = client.get("/publications/KA")
    match = re.search(r'_csrf_token["\s:]+([A-Za-z0-9_-]+)', page.text)
    assert match, "no CSRF token rendered on the publication page"
    return match.group(1)


def test_cancel_releases_an_issue_stuck_in_queued(client: TestClient):
    """A queued issue with no live job must be releasable from the UI."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        repo.mark_issue_queued(issue_id)

    token = _csrf_for(client)
    resp = client.post(
        "/publications/KA/issues/ka01/cancel", data={"_csrf_token": token}
    )
    assert resp.status_code == 200

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).status == "new"


def test_cancel_also_finishes_the_job_behind_the_issue(client: TestClient):
    """Otherwise the scheduler would pick the job up right after."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue_id = repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        repo.mark_issue_queued(issue_id)
        repo.create_job("download", {"issue_id": issue_id})

    token = _csrf_for(client)
    client.post("/publications/KA/issues/ka01/cancel", data={"_csrf_token": token})

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.list_active_download_jobs() == []
        assert repo.count_jobs_by_status()["error"] == 1


def test_cancel_requires_csrf(client: TestClient):
    resp = client.post("/publications/KA/issues/ka01/cancel", data={})
    assert resp.status_code == 400


def test_issue_row_offers_cancel_while_queued(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.mark_issue_queued(
            repo.get_issue_by_code("ka01", repo.get_publication("KA").id).id
        )

    resp = client.get("/publications/KA/issues/ka01/row")
    assert resp.status_code == 200
    assert "/issues/ka01/cancel" in resp.text
    assert "disabled" not in resp.text


def test_watching_queues_the_back_catalogue(client: TestClient):
    """Watch must queue what is already known, not just future issues."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        for i in range(3):
            repo.upsert_issue(
                Issue(
                    custom_code=f"back{i}",
                    issue_name=f"Nr {i}",
                    issue_date="2023-01-01",
                ),
                pub.id,
            )

    token = _csrf_for(client)
    resp = client.post("/publications/KA/watch", data={"_csrf_token": token})
    assert resp.status_code == 200

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        # The three back-catalogue issues; ka01 is already downloaded.
        assert repo.count_jobs_by_status()["queued"] == 3
        assert repo.get_publication("KA").watched is True


def test_unwatching_queues_nothing(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_issue(
            Issue(custom_code="back0", issue_name="Nr 0", issue_date="2023-01-01"),
            repo.get_publication("KA").id,
        )

    token = _csrf_for(client)
    client.post("/publications/KA/unwatch", data={"_csrf_token": token})

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.count_jobs_by_status()["queued"] == 0
        assert repo.get_publication("KA").watched is False


_RATIO_RE = re.compile(r"(\d+)\s*<span class=\"dim\">/(\d+)</span>")


def test_watch_response_row_keeps_its_download_count(client: TestClient):
    """The HTMX-swapped row after Watch must still show the ratio.

    ``publication_row.html`` is rendered both by the list page and by
    this endpoint's response - a fix that only threads the count through
    ``list_publications()`` would leave this row blank (TASK-1338).
    """
    token = _csrf_for(client)
    resp = client.post("/publications/KA/watch", data={"_csrf_token": token})
    assert resp.status_code == 200
    # The seeded KA publication has exactly one issue (ka01), downloaded.
    match = _RATIO_RE.search(resp.text)
    assert match, resp.text
    assert (match.group(1), match.group(2)) == ("1", "1")


def test_unwatch_response_row_keeps_its_download_count(client: TestClient):
    token = _csrf_for(client)
    resp = client.post("/publications/KA/unwatch", data={"_csrf_token": token})
    assert resp.status_code == 200
    match = _RATIO_RE.search(resp.text)
    assert match, resp.text
    assert (match.group(1), match.group(2)) == ("1", "1")


def test_queue_missing_queues_without_watching(client: TestClient):
    """Catching up must not change the watch flag."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        for i in range(2):
            repo.upsert_issue(
                Issue(
                    custom_code=f"miss{i}",
                    issue_name=f"Nr {i}",
                    issue_date="2023-01-01",
                ),
                pub.id,
            )

    token = _csrf_for(client)
    resp = client.post("/publications/KA/queue-missing", data={"_csrf_token": token})
    assert resp.status_code == 200
    assert "Queued 2 issues" in resp.text

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.count_jobs_by_status()["queued"] == 2
        assert repo.get_publication("KA").watched is False


def test_queue_missing_reports_when_there_is_nothing_to_do(client: TestClient):
    token = _csrf_for(client)
    # ka01 is already downloaded, gh01 belongs to another publication.
    resp = client.post("/publications/KA/queue-missing", data={"_csrf_token": token})
    assert resp.status_code == 200
    assert "Nothing to queue" in resp.text


def test_queue_missing_requires_csrf(client: TestClient):
    assert client.post("/publications/KA/queue-missing", data={}).status_code == 400


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

_METRIC_LINE_RE = re.compile(
    r'^[a-zA-Z_][a-zA-Z0-9_]*(\{[a-zA-Z_][a-zA-Z0-9_]*="[^"]*"\})? -?\d+(\.\d+)?$'
)


def _parse_prometheus_text(body: str) -> dict[str, list[str]]:
    """Structurally validate the exposition format and group lines by family.

    Returns ``{metric_name: [help_line, type_line, data_line, ...]}`` in
    the order the family appeared. Raises AssertionError on any line that
    doesn't look like a HELP/TYPE comment or a well-formed sample.
    """
    families: dict[str, list[str]] = {}
    seen_type: dict[str, str] = {}
    seen_samples: set[str] = set()
    current: str | None = None

    for line in body.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            name = line.split()[2]
            assert name not in families, f"duplicate HELP for {name}"
            families[name] = [line]
            current = name
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(maxsplit=3)
            assert current == name, f"TYPE for {name} not right after its HELP"
            assert name not in seen_type, f"duplicate TYPE for {name}"
            seen_type[name] = kind
            families[name].append(line)
            continue
        assert _METRIC_LINE_RE.match(line), f"malformed sample line: {line!r}"
        sample_name = line.split("{", 1)[0].split()[0]
        assert sample_name in seen_type, f"sample without preceding TYPE: {line!r}"
        assert line not in seen_samples, f"duplicate series: {line!r}"
        seen_samples.add(line)
        families[sample_name].append(line)

    return families


def test_metrics_is_valid_prometheus_text_format(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")

    families = _parse_prometheus_text(resp.text)
    assert set(families) == {
        "flipp_issues_total",
        "flipp_jobs_total",
        "flipp_publications_total",
        "flipp_publications_watched",
    }


def test_metrics_reflects_actual_counts(client: TestClient):
    # The client fixture seeds two publications (KA watched, GH not) and
    # marks one issue "done" on each of them.
    resp = client.get("/metrics")
    body = resp.text

    assert 'flipp_issues_total{status="done"} 2' in body
    assert 'flipp_issues_total{status="new"} 0' in body
    assert 'flipp_issues_total{status="error"} 0' in body
    assert 'flipp_jobs_total{status="queued"} 0' in body
    assert "flipp_publications_total 2" in body
    assert "flipp_publications_watched 0" in body


def test_metrics_excluded_from_openapi_schema(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert "/metrics" not in schema["paths"]
    assert "/healthz" not in schema["paths"]  # same pattern, sanity check


def test_metrics_requires_auth_when_password_is_set(
    tmp_path: Path, output_tree: Path, monkeypatch
):
    """/metrics is not in AuthMiddleware's public-path allowlist, so a
    scrape without a session is redirected to /login exactly like any
    other protected route when FLIPP_PASSWORD is configured."""
    from flipp_dl.web.app import create_app as _create_app

    monkeypatch.setenv("FLIPP_PASSWORD", "secret")
    app = _create_app(db_path=tmp_path / "auth.db", output_root=output_tree)
    protected_client = TestClient(app, follow_redirects=False)

    resp = protected_client.get("/metrics")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_queue_missing_unknown_publication_is_404(client: TestClient):
    token = _csrf_for(client)
    resp = client.post("/publications/NOPE/queue-missing", data={"_csrf_token": token})
    assert resp.status_code == 404


def test_metrics_can_be_scraped_without_login_when_opted_in(tmp_path, monkeypatch):
    """A Prometheus scraper cannot log in, so the flag must open /metrics."""
    monkeypatch.setenv("FLIPP_PASSWORD", "hemligt")
    monkeypatch.setenv("FLIPP_METRICS_PUBLIC", "1")
    app = create_app(db_path=tmp_path / "flipp.db", output_root=tmp_path / "out")
    with TestClient(app) as client:
        resp = client.get("/metrics", follow_redirects=False)
        assert resp.status_code == 200
        assert "flipp_issues_total" in resp.text
        # Everything else still requires login.
        assert client.get("/", follow_redirects=False).status_code == 302


def test_metrics_stays_closed_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("FLIPP_PASSWORD", "hemligt")
    monkeypatch.delenv("FLIPP_METRICS_PUBLIC", raising=False)
    app = create_app(db_path=tmp_path / "flipp.db", output_root=tmp_path / "out")
    with TestClient(app) as client:
        assert client.get("/metrics", follow_redirects=False).status_code == 302


# ---------------------------------------------------------------------------
# Settings – Flipp token (TASK-1342)
# ---------------------------------------------------------------------------


def test_settings_get_shows_no_token_configured_by_default(client: TestClient):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "No token configured" in resp.text


def test_settings_post_saves_token(client: TestClient):
    from flipp_dl.scheduler import resolve_current_token

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "sekret-token-123",
        },
    )
    assert resp.status_code == 200

    # The saved value never comes back in the response body.
    assert "sekret-token-123" not in resp.text

    # But it is what a scheduler tick would now use.
    assert resolve_current_token(client.app.state.session_factory) == "sekret-token-123"
    assert "A token is saved and in use" in resp.text


def test_settings_get_never_renders_saved_token(client: TestClient):
    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "another-secret-value",
        },
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "another-secret-value" not in resp.text
    assert "A token is saved and in use" in resp.text


def test_settings_post_empty_token_leaves_saved_token_unchanged(client: TestClient):
    from flipp_dl.scheduler import resolve_current_token

    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "original-token",
        },
    )

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "120",
            "workers": "2",
            "flipp_token": "",
        },
    )
    assert resp.status_code == 200
    assert resolve_current_token(client.app.state.session_factory) == "original-token"


def test_settings_post_requires_csrf(client: TestClient):
    resp = client.post(
        "/settings",
        data={
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "should-not-be-saved",
        },
    )
    assert resp.status_code == 400
    from flipp_dl.scheduler import resolve_current_token

    assert resolve_current_token(client.app.state.session_factory) == ""


def test_settings_post_saved_token_takes_priority_over_env(
    client: TestClient, monkeypatch
):
    from flipp_dl.scheduler import resolve_current_token

    monkeypatch.setenv("FLIPP_TOKEN", "env-token")
    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "ui-token",
        },
    )
    assert resolve_current_token(client.app.state.session_factory) == "ui-token"


def test_settings_get_shows_env_fallback_hint_when_no_token_saved(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("FLIPP_TOKEN", "env-token")
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "FLIPP_TOKEN" in resp.text
    assert "env-token" not in resp.text


# ---------------------------------------------------------------------------
# Import existing files (TASK-1283)
# ---------------------------------------------------------------------------


def test_import_existing_requires_csrf(client: TestClient):
    resp = client.post("/settings/import-existing")
    assert resp.status_code == 400


def test_import_existing_backfills_a_queued_issue_found_on_disk(
    client: TestClient, output_tree: Path
):
    from flipp_dl.models import Issue, Publication

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = Publication(
            custom_code="NEW",
            name="Kalle Anka",
            issues=[
                Issue(custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01")
            ],
        )
        db_pub = repo.upsert_publication(pub)
        db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
        repo.mark_issue_queued(db_issue.id)
        issue_id = db_issue.id

    from flipp_dl import storage

    target = storage.issue_path(output_tree, pub, pub.issues[0])
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    csrf = _csrf_for(client)
    resp = client.post("/settings/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "Backfilled" in resp.text
    assert "1" in resp.text

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).status == "done"


def test_import_existing_reports_orphan_files(client: TestClient, output_tree: Path):
    """The ``ka02.pdf`` file seeded by ``output_tree`` matches no issue."""
    csrf = _csrf_for(client)
    resp = client.post("/settings/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "orphans" in resp.text
    assert "ka02.pdf" in resp.text


def test_import_existing_reports_a_missing_file(client: TestClient):
    """The ``client`` fixture already seeds a ``done`` Ghost issue whose
    file was never written to disk."""
    csrf = _csrf_for(client)
    resp = client.post("/settings/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "missing on disk" in resp.text
    assert "Ghost" in resp.text
