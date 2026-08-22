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
def cover_cache_root(tmp_path: Path, monkeypatch) -> Path:
    """Point the cover cache at a temp dir well outside output_tree."""
    root = tmp_path / "covers"
    monkeypatch.setenv("FLIPP_COVER_CACHE", str(root))
    return root


@pytest.fixture()
def client(
    tmp_path: Path, output_tree: Path, cover_cache_root: Path, monkeypatch
) -> TestClient:
    """Spin up a fresh FastAPI app wired to a temp DB and output dir."""
    # Make sure no leftover FLIPP_PASSWORD forces auth on us.
    monkeypatch.delenv("FLIPP_PASSWORD", raising=False)

    db_path = tmp_path / "flipp.db"
    app = create_app(db_path=db_path, output_root=output_tree)

    # Seed a watched publication with one downloaded issue pointing at the
    # real file under output_tree so the file-serving route has something
    # to find. Also seed a cached cover for both the publication and the
    # issue (TASK-1345) - the cache is populated by the poll tick in
    # production, so tests write the file + DB pointer directly instead
    # of exercising the network fetch here.
    pub = Publication(
        custom_code="KA",
        name="Kalle Anka",
        cover_url="https://reader.flipp.se/covers/ka__b600m.jpg",
        issues=[Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01")],
    )
    cover_cache_root.mkdir(parents=True, exist_ok=True)
    (cover_cache_root / "pub-KA.jpg").write_bytes(b"\xff\xd8\xff-pub-cover")
    (cover_cache_root / "issue-ka01.jpg").write_bytes(b"\xff\xd8\xff-issue-cover")
    with get_session(app.state.session_factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(pub)
        db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
        repo.mark_issue_done(db_issue.id, str(output_tree / "Kalle Anka" / "ka01.pdf"))
        repo.set_publication_cover_cache(db_pub.id, "pub-KA.jpg", pub.cover_url)
        repo.set_issue_cover_cache(db_issue.id, "issue-ka01.jpg")

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


# ---------------------------------------------------------------------------
# Cover cache (TASK-1345)
# ---------------------------------------------------------------------------


def test_serve_publication_cover_returns_the_cached_file(client: TestClient):
    resp = client.get("/publications/KA/cover")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-pub-cover"


def test_serve_publication_cover_404_when_not_cached(client: TestClient):
    resp = client.get("/publications/GH/cover")
    assert resp.status_code == 404


def test_serve_publication_cover_404_for_unknown_publication(client: TestClient):
    resp = client.get("/publications/NOPE/cover")
    assert resp.status_code == 404


def test_serve_issue_cover_returns_the_cached_file(client: TestClient):
    resp = client.get("/publications/KA/issues/ka01/cover")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-issue-cover"


def test_serve_issue_cover_404_when_not_cached(client: TestClient):
    resp = client.get("/publications/GH/issues/gh01/cover")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cover lightbox (TASK-1396) - the larger, on-click-only variant
# ---------------------------------------------------------------------------


def test_serve_publication_cover_large_serves_existing_cache_without_fetching(
    client: TestClient, cover_cache_root: Path, monkeypatch
):
    (cover_cache_root / "pub-KA-large.jpg").write_bytes(b"\xff\xd8\xff-pub-large")

    def _boom(*args, **kwargs):
        raise AssertionError("must not fetch when already cached")

    monkeypatch.setattr("flipp_dl.web.routes.fetch_and_cache_cover", _boom)

    resp = client.get("/publications/KA/cover/large")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-pub-large"


def test_serve_publication_cover_large_fetches_on_demand(
    client: TestClient, cover_cache_root: Path, monkeypatch
):
    calls = []

    def _fake_fetch(url, cache_root, stem, **kwargs):
        calls.append((url, cache_root, stem))
        (cache_root / f"{stem}.jpg").write_bytes(b"\xff\xd8\xff-fetched")
        return f"{stem}.jpg"

    monkeypatch.setattr("flipp_dl.web.routes.fetch_and_cache_cover", _fake_fetch)

    resp = client.get("/publications/KA/cover/large")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-fetched"
    assert len(calls) == 1
    url, cache_root, stem = calls[0]
    # The stored cover_url already is the large (600m) variant - no size
    # juggling needed, unlike the thumbnail cache which downgrades it.
    assert url == "https://reader.flipp.se/covers/ka__b600m.jpg"
    assert stem == "pub-KA-large"


def test_serve_publication_cover_large_404_when_fetch_fails(
    client: TestClient, monkeypatch
):
    monkeypatch.setattr(
        "flipp_dl.web.routes.fetch_and_cache_cover", lambda *a, **k: None
    )
    resp = client.get("/publications/KA/cover/large")
    assert resp.status_code == 502


def test_serve_publication_cover_large_404_for_unknown_publication(
    client: TestClient,
):
    resp = client.get("/publications/NOPE/cover/large")
    assert resp.status_code == 404


def test_serve_issue_cover_large_serves_existing_cache_without_fetching(
    client: TestClient, cover_cache_root: Path, monkeypatch
):
    (cover_cache_root / "issue-ka01-large.jpg").write_bytes(b"\xff\xd8\xff-issue-large")

    def _boom(*args, **kwargs):
        raise AssertionError("must not fetch when already cached")

    monkeypatch.setattr("flipp_dl.web.routes.fetch_and_cache_cover", _boom)

    resp = client.get("/publications/KA/issues/ka01/cover/large")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-issue-large"


def test_serve_issue_cover_large_fetches_w600_on_demand(
    client: TestClient, cover_cache_root: Path, monkeypatch
):
    calls = []

    def _fake_fetch(url, cache_root, stem, **kwargs):
        calls.append((url, cache_root, stem))
        (cache_root / f"{stem}.jpg").write_bytes(b"\xff\xd8\xff-fetched")
        return f"{stem}.jpg"

    monkeypatch.setattr("flipp_dl.web.routes.fetch_and_cache_cover", _fake_fetch)

    resp = client.get("/publications/KA/issues/ka01/cover/large")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8\xff-fetched"
    assert len(calls) == 1
    url, _cache_root, stem = calls[0]
    # w=600 is the largest width pagesuite serves before 403ing - w=1200
    # was measured and confirmed forbidden.
    assert url == (
        "https://edition.pagesuite-professional.co.uk/get_image.aspx" "?w=600&eid=ka01"
    )
    assert stem == "issue-ka01-large"


def test_serve_issue_cover_large_404_for_unknown_issue(client: TestClient):
    resp = client.get("/publications/KA/issues/nope/cover/large")
    assert resp.status_code == 404


def test_serve_issue_cover_large_404_for_unknown_publication(client: TestClient):
    resp = client.get("/publications/NOPE/issues/ka01/cover/large")
    assert resp.status_code == 404


def test_publication_row_cover_links_to_lightbox(client: TestClient):
    resp = client.get("/publications")
    assert resp.status_code == 200
    assert 'data-lightbox-src="/publications/KA/cover/large"' in resp.text


def test_issue_row_cover_links_to_lightbox(client: TestClient):
    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert 'data-lightbox-src="/publications/KA/issues/ka01/cover/large"' in resp.text


def test_publication_detail_cover_links_to_lightbox(client: TestClient):
    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert 'data-lightbox-src="/publications/KA/cover/large"' in resp.text


def test_publications_list_never_hotlinks_pagesuite_or_flipp(client: TestClient):
    """The whole point of TASK-1345: no direct requests to the source CDN."""
    resp = client.get("/publications")
    assert resp.status_code == 200
    assert "pagesuite" not in resp.text.lower()
    assert "reader.flipp.se" not in resp.text
    assert "/publications/KA/cover" in resp.text


def test_publication_detail_never_hotlinks_and_uses_local_cover(client: TestClient):
    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert "pagesuite" not in resp.text.lower()
    assert "reader.flipp.se" not in resp.text
    assert "/publications/KA/cover" in resp.text
    assert "/publications/KA/issues/ka01/cover" in resp.text


def test_publication_detail_shows_read_badge_and_filter(client: TestClient):
    from datetime import datetime

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        repo.set_komga_book_id(issue.id, 42)
        repo.set_issue_read_status(
            issue.id, read=True, page=24, synced_at=datetime(2026, 8, 19)
        )

    resp = client.get("/publications/KA")

    assert resp.status_code == 200
    assert 'id="issue-read-filter"' in resp.text
    assert 'data-read-status="read"' in resp.text
    assert "Read" in resp.text or "Läst" in resp.text


def test_publication_detail_hides_read_badge_without_book_mapping(client: TestClient):
    # No komga_book_id set for ka01 in the base fixture - no crash, and
    # the row must not claim a read/unread status it doesn't have.
    resp = client.get("/publications/KA")

    assert resp.status_code == 200
    assert 'data-read-status=""' in resp.text


# ---------------------------------------------------------------------------
# Queue-missing size estimate in the confirm dialog (TASK-1362)
# ---------------------------------------------------------------------------


def test_queue_missing_confirm_shows_count_and_size(client: TestClient):
    """ka01 is downloaded with a known size - it's the basis for the estimate."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        issue.file_size = 50 * 1024 * 1024  # 50 MB
        for i in range(3):
            repo.upsert_issue(
                Issue(
                    custom_code=f"miss{i}",
                    issue_name=f"Nr {i}",
                    issue_date="2023-01-01",
                ),
                pub.id,
            )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert 'hx-confirm="Queue 3 missing issues (~150.0 MB)?"' in resp.text


def test_queue_missing_confirm_shows_count_only_without_size_data(client: TestClient):
    """No publication anywhere has a known file_size - no fabricated number."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        # The seeded ka01 issue is "done" but has no file_size recorded
        # (pre-TASK-1362 data) - and it's the only "done" issue in the DB.
        repo.upsert_issue(
            Issue(custom_code="miss0", issue_name="Nr 0", issue_date="2023-01-01"),
            pub.id,
        )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert 'hx-confirm="Queue 1 missing issue?"' in resp.text


def test_queue_missing_confirm_says_nothing_to_queue_when_all_downloaded(
    client: TestClient,
):
    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert (
        'hx-confirm="Nothing to queue - every issue is already downloaded."'
        in resp.text
    )


# ---------------------------------------------------------------------------
# Backfill warning threshold (TASK-1361)
# ---------------------------------------------------------------------------


def test_queue_missing_confirm_warns_above_default_threshold(client: TestClient):
    """Above 5 GiB the confirm dialog must spell out the threshold too."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        issue.file_size = 6 * 1024**3  # 6 GiB average - one missing issue exceeds 5 GiB
        repo.upsert_issue(
            Issue(custom_code="miss0", issue_name="Nr 0", issue_date="2023-01-01"),
            pub.id,
        )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert "above the 5.0 GB warning threshold" in resp.text
    assert "6.0 GB" in resp.text


def test_queue_missing_confirm_env_threshold_is_honoured(
    client: TestClient, monkeypatch
):
    """A lower FLIPP_QUEUE_WARN_THRESHOLD_BYTES trips the warning sooner."""
    monkeypatch.setenv("FLIPP_QUEUE_WARN_THRESHOLD_BYTES", str(100 * 1024 * 1024))
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        issue.file_size = 50 * 1024 * 1024  # 50 MB
        for i in range(3):
            repo.upsert_issue(
                Issue(
                    custom_code=f"miss{i}",
                    issue_name=f"Nr {i}",
                    issue_date="2023-01-01",
                ),
                pub.id,
            )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    # 3 issues * 50 MB = 150 MB > 100 MB threshold.
    assert "above the 100.0 MB warning threshold" in resp.text


def test_queue_missing_confirm_stays_below_threshold_by_default(client: TestClient):
    """150 MB (TASK-1362's own fixture) must not trip the 5 GiB default."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        issue.file_size = 50 * 1024 * 1024
        for i in range(3):
            repo.upsert_issue(
                Issue(
                    custom_code=f"miss{i}",
                    issue_name=f"Nr {i}",
                    issue_date="2023-01-01",
                ),
                pub.id,
            )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert "warning threshold" not in resp.text
    assert 'hx-confirm="Queue 3 missing issues (~150.0 MB)?"' in resp.text


def test_queue_missing_confirm_does_not_warn_without_size_data(client: TestClient):
    """No size known - nothing to compare to the threshold, no fabrication."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        repo.upsert_issue(
            Issue(custom_code="miss0", issue_name="Nr 0", issue_date="2023-01-01"),
            pub.id,
        )

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert "warning threshold" not in resp.text
    assert 'hx-confirm="Queue 1 missing issue?"' in resp.text


def test_issue_row_never_hotlinks_pagesuite(client: TestClient):
    resp = client.get("/publications/KA/issues/ka01/row")
    assert resp.status_code == 200
    assert "pagesuite" not in resp.text.lower()
    assert "/publications/KA/issues/ka01/cover" in resp.text


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


def test_publications_list_shows_delisted_marker(client: TestClient):
    """TASK-1426: a publication missing from the latest poll is flagged.

    The wording must say "no longer listed", never "unavailable" or
    "removed" - the issue is still fetchable directly, it just isn't
    offered by the API's publication list any more.
    """
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        stitch = Publication(
            custom_code="STITCH",
            name="Stitch",
            issues=[
                Issue(
                    custom_code="stitch-01", issue_name="Nr 3", issue_date="2026-01-01"
                )
            ],
        )
        repo.sync_publications([stitch])
        # Next poll's response no longer contains Stitch or KA... except
        # KA must stay present so it does NOT get flagged too.
        ka = repo.get_publication("KA")
        repo.sync_publications([Publication(custom_code="KA", name=ka.name, issues=[])])

    resp = client.get("/publications?watched=0")
    assert resp.status_code == 200
    assert "No longer listed by Flipp" in resp.text
    assert 'data-code="stitch"' in resp.text
    stitch_row_start = resp.text.index('id="pub-row-STITCH"')
    stitch_row_end = resp.text.index("</tr>", stitch_row_start)
    stitch_row = resp.text[stitch_row_start:stitch_row_end]
    assert 'data-delisted="1"' in stitch_row

    ka_row_start = resp.text.index('id="pub-row-KA"')
    ka_row_end = resp.text.index("</tr>", ka_row_start)
    ka_row = resp.text[ka_row_start:ka_row_end]
    assert 'data-delisted="0"' in ka_row
    # Never phrase this as removed/unavailable - it is still downloadable.
    assert "unavailable" not in resp.text.lower()
    assert "removed" not in resp.text.lower()


def test_publication_detail_shows_delisted_issue_marker(client: TestClient):
    """TASK-1429: an issue missing from the latest poll is flagged, on the
    publication's own detail page, without touching the "no longer
    listed" publication-level marker (KA itself stays listed).

    Wording must say "no longer listed", matching the publication-level
    convention - never "unavailable" or "removed".
    """
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        ka = repo.get_publication("KA")
        # ka01 (seeded by the fixture) stays listed; ka02 is added first
        # and then dropped from the next poll's response.
        pub_with_two = Publication(
            custom_code="KA",
            name=ka.name,
            issues=[
                Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01"),
                Issue(custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01"),
            ],
        )
        repo.sync_publications([pub_with_two])
        # Next poll only returns ka01 - ka02 drops out.
        pub_with_one = Publication(
            custom_code="KA",
            name=ka.name,
            issues=[
                Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01"),
            ],
        )
        repo.sync_publications([pub_with_one])

    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert "No longer listed" in resp.text
    assert "1 no longer listed" in resp.text

    ka01_start = resp.text.index('id="issue-row-ka01"')
    ka01_end = resp.text.index("</tr>", ka01_start)
    assert 'data-delisted="0"' in resp.text[ka01_start:ka01_end]

    ka02_start = resp.text.index('id="issue-row-ka02"')
    ka02_end = resp.text.index("</tr>", ka02_start)
    ka02_row = resp.text[ka02_start:ka02_end]
    assert 'data-delisted="1"' in ka02_row

    # The KA publication itself is still listed - only the issue is flagged.
    assert 'data-delisted="1"' not in resp.text[: resp.text.index("<h1>")]
    assert "No longer listed only" in resp.text
    assert "unavailable" not in resp.text.lower()
    assert "removed" not in resp.text.lower()


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


def test_watching_does_not_queue_the_back_catalogue(client: TestClient):
    """TASK-1361: Watch bevakar framåt only - it must queue nothing itself.

    Replaces the old test_watching_queues_the_back_catalogue, which
    asserted the opposite (TASK-1346 behaviour): that is exactly the
    "queue everything by accident" bug this task fixes. Fetching the back
    catalogue is now the dedicated "Queue missing issues" button's job.
    """
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
        assert repo.count_jobs_by_status()["queued"] == 0
        pub = repo.get_publication("KA")
        assert pub.watched is True
        assert pub.watch_started_at is not None


def test_second_same_named_publication_requires_own_folder_before_watch(
    client: TestClient,
):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(Publication(custom_code="HJ-NO", name="Hjemmet"))
        repo.upsert_publication(Publication(custom_code="HJ-DK", name="Hjemmet"))

    token = _csrf_for(client)
    response = client.post("/publications/HJ-DK/watch", data={"_csrf_token": token})

    assert response.status_code == 200
    assert 'name="folder_name"' in response.text
    assert "Choose a unique folder name before watching" in response.text
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("HJ-DK").watched is False

    response = client.post(
        "/publications/HJ-DK/watch",
        data={"_csrf_token": token, "folder_name": "Hjemmet (DK)"},
    )
    assert response.status_code == 200
    with get_session(client.app.state.session_factory) as session:
        publication = DownloadRepository(session).get_publication("HJ-DK")
        assert publication.watched is True
        assert publication.folder_name == "Hjemmet (DK)"


def test_watch_rejects_invalid_or_colliding_own_folder_name(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(Publication(custom_code="HJ-NO", name="Hjemmet"))
        repo.upsert_publication(Publication(custom_code="HJ-DK", name="Hjemmet"))
        repo.set_publication_folder_name(
            "HJ-NO", "Hjemmet (NO)", client.app.state.output_root
        )

    token = _csrf_for(client)
    invalid = client.post(
        "/publications/HJ-DK/watch",
        data={"_csrf_token": token, "folder_name": "../Hjemmet"},
    )
    assert "Use only characters" in invalid.text

    conflict = client.post(
        "/publications/HJ-DK/watch",
        data={"_csrf_token": token, "folder_name": "Hjemmet (NO)"},
    )
    assert "already used by another publication" in conflict.text
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("HJ-DK").watched is False


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


# ---------------------------------------------------------------------------
# Size-on-disk column (TASK-1379)
# ---------------------------------------------------------------------------


def test_publications_list_shows_known_size(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        issue.file_size = 50 * 1024 * 1024  # 50 MB

    resp = client.get("/publications")
    assert resp.status_code == 200
    assert "50.0 MB" in resp.text
    # A fully-known size shows plain, without the partial-total tilde.
    assert "~50.0 MB" not in resp.text


def test_publications_list_shows_unknown_for_unsized_downloads(client: TestClient):
    """The seeded ka01 issue is done but predates TASK-1362's file_size -
    a downloaded publication must never read as "0 B" on disk.
    """
    resp = client.get("/publications")
    assert resp.status_code == 200
    assert "unknown" in resp.text or "okänd" in resp.text
    assert "0 B" not in resp.text


def test_publications_list_flags_partial_total_with_tilde(client: TestClient):
    """One sized issue plus one unsized issue -> a partial, flagged total."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issue = repo.get_issue_by_code("ka01", pub.id)
        issue.file_size = 10 * 1024 * 1024  # 10 MB, known
        second = Issue(custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01")
        db_issue, _ = repo.upsert_issue(second, pub.id)
        repo.mark_issue_done(db_issue.id, "/tmp/ka02.pdf")  # file_size stays None

    resp = client.get("/publications")
    assert resp.status_code == 200
    assert "~10.0 MB" in resp.text


def test_publications_list_shows_dash_when_nothing_downloaded(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.upsert_publication(
            Publication(custom_code="NEWPUB", name="New", issues=[])
        )
        repo.upsert_issue(
            Issue(custom_code="np01", issue_name="Nr 1", issue_date="2024-01-01"),
            repo.get_publication("NEWPUB").id,
        )

    resp = client.get("/publications")
    assert resp.status_code == 200
    match = re.search(r'<tr\s+id="pub-row-NEWPUB".*?</tr>', resp.text, re.DOTALL)
    assert match, resp.text
    row_html = match.group(0)
    # Not downloaded at all - no size total, and definitely no fabricated
    # "0 B" for a publication that hasn't downloaded a single issue.
    assert "0 B" not in row_html
    assert "—" in row_html


def test_watch_response_row_keeps_its_size(client: TestClient):
    """The HTMX-swapped row after Watch must still show the size total -
    same failure mode as the ratio in TASK-1338, now for size (TASK-1379).
    """
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        issue.file_size = 50 * 1024 * 1024

    token = _csrf_for(client)
    resp = client.post("/publications/KA/watch", data={"_csrf_token": token})
    assert resp.status_code == 200
    assert "50.0 MB" in resp.text


def test_unwatch_response_row_keeps_its_size(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka01", repo.get_publication("KA").id)
        issue.file_size = 50 * 1024 * 1024

    token = _csrf_for(client)
    resp = client.post("/publications/KA/unwatch", data={"_csrf_token": token})
    assert resp.status_code == 200
    assert "50.0 MB" in resp.text


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
# Per-publication poll interval (TASK-1291)
# ---------------------------------------------------------------------------


def test_poll_interval_field_defaults_to_blank(client: TestClient):
    resp = client.get("/publications/KA")
    assert resp.status_code == 200
    assert 'name="poll_interval_minutes"' in resp.text
    assert 'value=""' in resp.text


def test_set_poll_interval_persists_and_shows_on_reload(client: TestClient):
    token = _csrf_for(client)
    resp = client.post(
        "/publications/KA/poll-interval",
        data={"_csrf_token": token, "poll_interval_minutes": "90"},
    )
    assert resp.status_code == 200  # follow_redirects default True in TestClient

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_publication("KA").poll_interval_minutes == 90

    reloaded = client.get("/publications/KA")
    assert 'value="90"' in reloaded.text


def test_set_poll_interval_blank_clears_override(client: TestClient):
    token = _csrf_for(client)
    client.post(
        "/publications/KA/poll-interval",
        data={"_csrf_token": token, "poll_interval_minutes": "90"},
    )
    client.post(
        "/publications/KA/poll-interval",
        data={"_csrf_token": token, "poll_interval_minutes": ""},
    )

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_publication("KA").poll_interval_minutes is None


def test_set_poll_interval_requires_csrf(client: TestClient):
    resp = client.post(
        "/publications/KA/poll-interval", data={"poll_interval_minutes": "90"}
    )
    assert resp.status_code == 400


def test_set_poll_interval_rejects_non_numeric(client: TestClient):
    token = _csrf_for(client)
    resp = client.post(
        "/publications/KA/poll-interval",
        data={"_csrf_token": token, "poll_interval_minutes": "abc"},
    )
    assert resp.status_code == 400


def test_set_poll_interval_unknown_publication_404(client: TestClient):
    token = _csrf_for(client)
    resp = client.post(
        "/publications/NOPE/poll-interval",
        data={"_csrf_token": token, "poll_interval_minutes": "90"},
    )
    assert resp.status_code == 404


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


def test_settings_get_shows_default_queue_warn_threshold_gb(client: TestClient):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'name="queue_warn_threshold_gb"' in resp.text
    assert 'value="5"' in resp.text


def test_settings_post_saves_queue_warn_threshold_gb(client: TestClient):
    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "queue_warn_threshold_gb": "2",
        },
    )
    assert resp.status_code == 200
    assert 'value="2"' in resp.text

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.queue_warn_threshold_bytes() == 2 * 1024**3

    # Survives a reload, not just the immediate response.
    resp = client.get("/settings")
    assert 'value="2"' in resp.text


def test_settings_post_blank_queue_warn_threshold_resets_to_default(
    client: TestClient,
):
    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "queue_warn_threshold_gb": "2",
        },
    )

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "queue_warn_threshold_gb": "",
        },
    )
    assert resp.status_code == 200

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.queue_warn_threshold_bytes() == 5 * 1024**3


def test_settings_post_rejects_non_numeric_queue_warn_threshold(client: TestClient):
    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "queue_warn_threshold_gb": "abc",
        },
    )
    assert resp.status_code == 400

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.queue_warn_threshold_bytes() == 5 * 1024**3


def test_settings_post_rejects_zero_or_negative_queue_warn_threshold(
    client: TestClient,
):
    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "queue_warn_threshold_gb": "0",
        },
    )
    assert resp.status_code == 400

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.queue_warn_threshold_bytes() == 5 * 1024**3


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
# Settings - Komga integration (TASK-1326)
# ---------------------------------------------------------------------------


def test_settings_get_shows_komga_section_disabled_by_default(client: TestClient):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'id="komga_enabled"' in resp.text
    assert "checked" not in resp.text.split('id="komga_enabled"')[1].split(">")[0]


def test_settings_post_saves_komga_fields(client: TestClient):
    from flipp_dl.scheduler import resolve_komga_settings

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "",
            "komga_enabled": "on",
            "komga_url": "http://localhost:25600",
            "komga_username": "alice",
            "komga_password": "sekret-pw",
            "komga_api_key": "",
            "komga_library_id": "lib-1",
        },
    )
    assert resp.status_code == 200

    # Secrets never come back in the response body.
    assert "sekret-pw" not in resp.text

    settings = resolve_komga_settings(client.app.state.session_factory)
    assert settings["enabled"] is True
    assert settings["url"] == "http://localhost:25600"
    assert settings["username"] == "alice"
    assert settings["password"] == "sekret-pw"
    assert settings["library_id"] == "lib-1"


def test_settings_get_never_renders_saved_komga_password(client: TestClient):
    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "",
            "komga_enabled": "on",
            "komga_url": "http://localhost:25600",
            "komga_username": "alice",
            "komga_password": "another-secret",
            "komga_api_key": "",
            "komga_library_id": "lib-1",
        },
    )

    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "another-secret" not in resp.text
    assert 'placeholder="•••"' in resp.text


def test_settings_post_empty_komga_password_leaves_saved_value_unchanged(
    client: TestClient,
):
    from flipp_dl.scheduler import resolve_komga_settings

    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "",
            "komga_enabled": "on",
            "komga_url": "http://localhost:25600",
            "komga_username": "alice",
            "komga_password": "original-pw",
            "komga_api_key": "",
            "komga_library_id": "lib-1",
        },
    )

    csrf = _csrf_for(client)
    client.post(
        "/settings",
        data={
            "_csrf_token": csrf,
            "poll_interval": "360",
            "workers": "4",
            "flipp_token": "",
            "komga_enabled": "on",
            "komga_url": "http://localhost:25600",
            "komga_username": "alice",
            "komga_password": "",
            "komga_api_key": "",
            "komga_library_id": "lib-1",
        },
    )

    settings = resolve_komga_settings(client.app.state.session_factory)
    assert settings["password"] == "original-pw"


def test_settings_disabled_by_default_no_komga_work(client: TestClient):
    """Nothing saved yet - the effective settings must resolve to disabled."""
    from flipp_dl.scheduler import resolve_komga_settings

    settings = resolve_komga_settings(client.app.state.session_factory)
    assert settings["enabled"] is False


def test_komga_test_connection_populates_library_dropdown(
    client: TestClient, monkeypatch
):
    class FakeKomgaClient:
        def __init__(self, url, *, username="", password="", api_key=""):
            self.url = url

        def list_libraries(self):
            return [{"id": "lib-1", "name": "Comics"}, {"id": "lib-2", "name": "Manga"}]

    monkeypatch.setattr("flipp_dl.web.routes.KomgaClient", FakeKomgaClient)

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings/komga/test",
        data={
            "_csrf_token": csrf,
            "komga_url": "http://localhost:25600",
            "komga_username": "",
            "komga_password": "",
            "komga_api_key": "test-key",
        },
    )
    assert resp.status_code == 200
    assert "Comics" in resp.text
    assert "Manga" in resp.text
    assert 'value="lib-1"' in resp.text


def test_komga_test_connection_shows_error_on_failure(client: TestClient, monkeypatch):
    from flipp_dl.komga import KomgaError

    class FailingKomgaClient:
        def __init__(self, url, *, username="", password="", api_key=""):
            pass

        def list_libraries(self):
            # The raw text stays in the log; the user gets the short form.
            raise KomgaError("connection refused", reason="unreachable")

    monkeypatch.setattr("flipp_dl.web.routes.KomgaClient", FailingKomgaClient)

    csrf = _csrf_for(client)
    resp = client.post(
        "/settings/komga/test",
        data={
            "_csrf_token": csrf,
            "komga_url": "http://localhost:25600",
            "komga_username": "",
            "komga_password": "",
            "komga_api_key": "test-key",
        },
    )
    assert resp.status_code == 200
    assert "the address did not respond" in resp.text
    # The requests-level detail must not reach the page (TASK-1388).
    assert "connection refused" not in resp.text


def test_komga_test_connection_requires_csrf(client: TestClient):
    resp = client.post(
        "/settings/komga/test",
        data={
            "komga_url": "http://localhost:25600",
            "komga_username": "",
            "komga_password": "",
            "komga_api_key": "test-key",
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Import existing files (TASK-1283)
# ---------------------------------------------------------------------------


def test_import_existing_requires_csrf(client: TestClient):
    resp = client.post("/library/import-existing")
    assert resp.status_code == 400


def test_import_existing_backfills_a_queued_issue_found_on_disk(
    client: TestClient, output_tree: Path
):
    from flipp_dl.models import Issue, Publication

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        # A distinct name on purpose: two publications sharing a name
        # share a folder, and since TASK-1404 the import refuses to
        # guess which one a file belongs to. That case has its own test.
        pub = Publication(
            custom_code="NEW",
            name="Kalle Anka Extra",
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    csrf = _csrf_for(client)
    resp = client.post("/library/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "Backfilled" in resp.text
    assert "1" in resp.text

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).status == "done"


def test_import_existing_reports_orphan_files(client: TestClient, output_tree: Path):
    """The ``ka02.pdf`` file seeded by ``output_tree`` matches no issue."""
    csrf = _csrf_for(client)
    resp = client.post("/library/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "orphans" in resp.text
    assert "ka02.pdf" in resp.text


def test_import_existing_reports_a_missing_file(client: TestClient):
    """The ``client`` fixture already seeds a ``done`` Ghost issue whose
    file was never written to disk."""
    csrf = _csrf_for(client)
    resp = client.post("/library/import-existing", data={"_csrf_token": csrf})
    assert resp.status_code == 200
    assert "missing on disk" in resp.text
    assert "Ghost" in resp.text


# ---------------------------------------------------------------------------
# Preview (TASK-1344)
# ---------------------------------------------------------------------------


def _one_page_pdf() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakeFlippClient:
    """Stand-in for FlippClient used by the /preview route tests."""

    instances: list[_FakeFlippClient] = []

    def __init__(self, token: str) -> None:
        self.token = token
        _FakeFlippClient.instances.append(self)

    def fetch_issue_pdf_urls(self, _pub_code: str, _issue_code: str) -> list[str]:
        return [f"http://example.invalid/p{i}.pdf" for i in range(5)]

    def download_pdf(self, _url: str) -> bytes:
        return _one_page_pdf()


@pytest.fixture()
def preview_client(client: TestClient, tmp_path: Path, monkeypatch) -> TestClient:
    """The shared ``client`` fixture, with a token saved and a fake Flipp client."""
    _FakeFlippClient.instances.clear()
    monkeypatch.setattr("flipp_dl.web.routes.FlippClient", _FakeFlippClient)
    preview_root = tmp_path / "previews"
    monkeypatch.setattr(
        "flipp_dl.downloader.default_preview_root", lambda: preview_root
    )

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.set_setting("flipp_token", "test-token")
        # A second, not-yet-downloaded issue to preview.
        pub = repo.get_publication("KA")
        new_issue = Issue(
            custom_code="ka02", issue_name="Nr 2", issue_date="2024-02-01"
        )
        repo.upsert_issue(new_issue, pub.id)

    return client


def test_preview_returns_pdf_inline(preview_client: TestClient):
    resp = preview_client.get("/publications/KA/issues/ka02/preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "inline" in resp.headers.get("content-disposition", "").lower()
    assert resp.content.startswith(b"%PDF")


def test_preview_only_fetches_a_few_pages(preview_client: TestClient):
    resp = preview_client.get("/publications/KA/issues/ka02/preview")
    assert resp.status_code == 200
    # 5 pages are available upstream; the preview only asked for 3.
    from flipp_dl.downloader import DEFAULT_PREVIEW_PAGES

    assert DEFAULT_PREVIEW_PAGES < 5


def test_preview_leaves_issue_status_untouched(preview_client: TestClient):
    resp = preview_client.get("/publications/KA/issues/ka02/preview")
    assert resp.status_code == 200

    with get_session(preview_client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        issue = repo.get_issue_by_code("ka02", repo.get_publication("KA").id)
        assert issue.status == "new"
        assert issue.file_path is None


def test_preview_does_not_create_a_job(preview_client: TestClient):
    with get_session(preview_client.app.state.session_factory) as session:
        before = len(DownloadRepository(session).list_jobs(limit=100))

    resp = preview_client.get("/publications/KA/issues/ka02/preview")
    assert resp.status_code == 200

    with get_session(preview_client.app.state.session_factory) as session:
        after = len(DownloadRepository(session).list_jobs(limit=100))
    assert after == before


def test_preview_writes_outside_output_root(preview_client: TestClient, tmp_path: Path):
    resp = preview_client.get("/publications/KA/issues/ka02/preview")
    assert resp.status_code == 200

    output_root = preview_client.app.state.output_root
    preview_root = tmp_path / "previews"
    files = list(preview_root.glob("preview-*.pdf"))
    assert len(files) == 1
    assert output_root.resolve() not in files[0].resolve().parents


def test_preview_requires_a_configured_token(client: TestClient, monkeypatch):
    monkeypatch.setattr("flipp_dl.web.routes.FlippClient", _FakeFlippClient)
    resp = client.get("/publications/KA/issues/ka01/preview")
    assert resp.status_code == 400


def test_preview_404_for_unknown_publication(preview_client: TestClient):
    resp = preview_client.get("/publications/NOPE/issues/ka02/preview")
    assert resp.status_code == 404


def test_preview_404_for_unknown_issue(preview_client: TestClient):
    resp = preview_client.get("/publications/KA/issues/nope/preview")
    assert resp.status_code == 404


def test_issue_row_has_a_preview_link(client: TestClient):
    resp = client.get("/publications/KA/issues/ka01/row")
    assert resp.status_code == 200
    assert "/publications/KA/issues/ka01/preview" in resp.text


def test_web_entrypoint_registers_every_scheduled_job():
    """The Docker entrypoint must schedule the same jobs as the CLI.

    A job registered only in build_scheduler() never runs in production,
    which is exactly what happened to the daily Komga read-status sync
    (TASK-1328).
    """
    import ast
    from pathlib import Path

    def job_ids(source: Path) -> set[str]:
        tree = ast.parse(source.read_text())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr != "add_job":
                    continue
                for kw in node.keywords:
                    if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                        found.add(kw.value.value)
        return found

    cli_jobs = job_ids(Path("flipp_dl/scheduler.py"))
    web_jobs = job_ids(Path("flipp_dl/web/main.py"))
    assert cli_jobs, "no jobs found in scheduler.py - has add_job been renamed?"
    assert cli_jobs <= web_jobs, f"only scheduled in the CLI: {cli_jobs - web_jobs}"


# ---------------------------------------------------------------------------
# Cross-publication search (TASK-1364)
# ---------------------------------------------------------------------------


def test_search_shows_prompt_without_any_query(client: TestClient):
    """No q/status/downloaded at all - the page must not run a search."""
    resp = client.get("/search")
    assert resp.status_code == 200
    assert "Kalle Anka" not in resp.text


def test_search_finds_issue_by_name_across_publications(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        other = repo.upsert_publication(
            Publication(custom_code="DD", name="Dagens Dax")
        )
        repo.upsert_issue(
            Issue(
                custom_code="dd01",
                issue_name="Special jubileum",
                issue_date="2025-06-01",
            ),
            other.id,
        )

    resp = client.get("/search", params={"q": "jubileum"})
    assert resp.status_code == 200
    assert "Special jubileum" in resp.text
    assert "Dagens Dax" in resp.text
    # The seeded KA issue does not match "jubileum" - must not show up.
    assert "Nr 1" not in resp.text


def test_search_matches_publication_name_not_just_issue_name(client: TestClient):
    """A query naming the publication finds its issues too."""
    resp = client.get("/search", params={"q": "Kalle Anka"})
    assert resp.status_code == 200
    assert "Nr 1" in resp.text


def test_search_filters_by_status(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        errored, _ = repo.upsert_issue(
            Issue(custom_code="ka-err", issue_name="Trasig", issue_date="2025-01-01"),
            pub.id,
        )
        errored.status = "error"

    resp = client.get("/search", params={"q": "Kalle", "status": "error"})
    assert resp.status_code == 200
    assert "Trasig" in resp.text
    assert "Nr 1" not in resp.text  # ka01 is "done", filtered out


def test_search_filters_by_retry_pending_status(client: TestClient):
    """TASK-1363 added retry_pending - the search status filter must offer it."""
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        retry_issue, _ = repo.upsert_issue(
            Issue(custom_code="ka-retry", issue_name="Väntar", issue_date="2025-02-01"),
            pub.id,
        )
        retry_issue.status = "retry_pending"

    resp = client.get("/search")
    assert 'value="retry_pending"' in resp.text

    resp = client.get("/search", params={"status": "retry_pending"})
    assert resp.status_code == 200
    assert "Väntar" in resp.text
    assert "Nr 1" not in resp.text


def test_search_filters_by_downloaded_only(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        repo.upsert_issue(
            Issue(custom_code="ka-new", issue_name="Ny", issue_date="2025-03-01"),
            pub.id,
        )

    resp = client.get("/search", params={"q": "Kalle", "downloaded": "yes"})
    assert resp.status_code == 200
    assert "Nr 1" in resp.text  # ka01 is done
    assert "Ny" not in resp.text  # ka-new is "new"


def test_search_filters_by_not_downloaded_only(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        repo.upsert_issue(
            Issue(custom_code="ka-new", issue_name="Ny", issue_date="2025-03-01"),
            pub.id,
        )

    resp = client.get("/search", params={"q": "Kalle", "downloaded": "no"})
    assert resp.status_code == 200
    assert "Ny" in resp.text
    assert "Nr 1" not in resp.text


def test_search_links_downloaded_issue_directly_to_its_pdf(client: TestClient):
    resp = client.get("/search", params={"q": "Kalle"})
    assert resp.status_code == 200
    assert "/publications/KA/issues/ka01/file" in resp.text


def test_search_links_undownloaded_issue_to_publication_detail(client: TestClient):
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        repo.upsert_issue(
            Issue(custom_code="ka-new", issue_name="Färsk", issue_date="2025-04-01"),
            pub.id,
        )

    resp = client.get("/search", params={"q": "Färsk"})
    assert resp.status_code == 200
    assert "/publications/KA#issue-row-ka-new" in resp.text
    assert "/publications/KA/issues/ka-new/file" not in resp.text


def test_search_query_count_does_not_grow_with_number_of_issues(client: TestClient):
    """TASK-1364: search must be one bounded DB query, not "load all, filter
    in Python" - the same trap TASK-1338 fixed for the publication list.

    Seeds a small batch of matching issues, records the SELECT count and
    checks each SELECT touching ``issues`` carries a LIMIT; then seeds a
    much larger batch and asserts the SELECT count for the exact same
    request is unchanged.
    """
    from sqlalchemy import event

    engine = client.app.state.session_factory.kw["bind"]

    def _select_count_for_search() -> list[str]:
        statements: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _capture)
        try:
            resp = client.get("/search", params={"q": "Ymer"})
        finally:
            event.remove(engine, "before_cursor_execute", _capture)
        assert resp.status_code == 200
        return [s for s in statements if s.strip().upper().startswith("SELECT")]

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.upsert_publication(Publication(custom_code="YM", name="Ymer"))
        for i in range(5):
            repo.upsert_issue(
                Issue(
                    custom_code=f"ym{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
                ),
                pub.id,
            )

    small_selects = _select_count_for_search()
    issues_selects = [s for s in small_selects if "FROM issues" in s]
    assert issues_selects, "expected the search query to hit the issues table"
    for stmt in issues_selects:
        assert "LIMIT" in stmt.upper(), stmt

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("YM")
        for i in range(5, 305):
            repo.upsert_issue(
                Issue(
                    custom_code=f"ym{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
                ),
                pub.id,
            )

    large_selects = _select_count_for_search()
    assert len(large_selects) == len(small_selects)


def test_search_result_capped_and_flags_more_available(client: TestClient, monkeypatch):
    """With more matches than the cap, the page says so instead of dumping them all."""
    monkeypatch.setattr(DownloadRepository, "SEARCH_ISSUE_LIMIT", 3)
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.upsert_publication(Publication(custom_code="CAP", name="Cap Test"))
        for i in range(6):
            repo.upsert_issue(
                Issue(
                    custom_code=f"cap{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
                ),
                pub.id,
            )

    resp = client.get("/search", params={"q": "Cap Test"})
    assert resp.status_code == 200
    # Only the row-count matters here, not which three of the six matched.
    assert resp.text.count("/publications/CAP#issue-row-cap") == 3
    assert (
        "Showing the first 3 matches" in resp.text or "Visar de första 3" in resp.text
    )


def test_search_nav_link_present_and_active(client: TestClient):
    resp = client.get("/search")
    assert resp.status_code == 200
    assert 'href="/search"' in resp.text


def test_dashboard_shows_issue_covers_in_recent_downloads(client: TestClient):
    """Recent downloads gets the same cover column as the other lists."""
    resp = client.get("/")
    assert resp.status_code == 200
    # ka01 is the seeded downloaded issue.
    assert "/publications/KA/issues/ka01/cover" in resp.text
    # And it opens the lightbox like covers elsewhere.
    assert "/publications/KA/issues/ka01/cover/large" in resp.text


def test_settings_shows_the_token_console_snippet(client: TestClient):
    """The snippet must read the cookie Flipp actually uses.

    Flipp's own web app stores the token as a cookie named flipp_token
    (verified against its main bundle), so the snippet reads that -
    not localStorage.
    """
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "flipp_token=" in resp.text
    assert "document.cookie" in resp.text
    assert "tidningar.flipp.se" in resp.text


def test_destination_change_is_refused_for_downloaded_publication_in_swedish(
    client: TestClient,
):
    client.get("/language/sv?next=/publications/KA")
    token = _csrf_for(client)

    response = client.post(
        "/publications/KA/destination",
        data={"_csrf_token": token, "destination": "secondary"},
    )

    assert response.status_code == 400
    assert (
        "Destinationen kan inte ändras eftersom publikationen redan har nedladdade utgåvor."
        in response.text
    )
    assert 'class="error destination-error"' in response.text
    with get_session(client.app.state.session_factory) as session:
        publication = DownloadRepository(session).get_publication("KA")
        assert publication.destination is None


def test_settings_saves_secondary_output_root_as_absolute_path(
    client: TestClient, tmp_path: Path
):
    selected = tmp_path / "secondary"
    selected.mkdir()
    token = _csrf_for(client)

    response = client.post(
        "/settings",
        data={
            "_csrf_token": token,
            "poll_interval": "360",
            "workers": "4",
            "secondary_output_root": str(selected),
        },
    )

    assert response.status_code == 200
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_setting("secondary_output_root") == str(
            selected.resolve()
        )


def test_settings_rejects_a_secondary_root_that_does_not_exist(
    client: TestClient, tmp_path: Path
):
    """A typo must not be stored - every download to it would fail later."""
    token = _csrf_for(client)

    response = client.post(
        "/settings",
        data={
            "_csrf_token": token,
            "poll_interval": "360",
            "workers": "4",
            "secondary_output_root": str(tmp_path / "finns-inte"),
        },
    )

    assert response.status_code == 200
    assert "That folder does not exist" in response.text
    assert "Settings saved successfully" not in response.text
    # The typed value stays on screen so it can be corrected.
    assert "finns-inte" in response.text
    with get_session(client.app.state.session_factory) as session:
        assert (
            DownloadRepository(session).get_setting("secondary_output_root", "") == ""
        )


# ---------------------------------------------------------------------------
# Per-publication notifications (TASK-1447)
# ---------------------------------------------------------------------------


def test_publication_notifications_are_off_until_turned_on(client: TestClient):
    token = _csrf_for(client)

    response = client.post(
        "/publications/KA/notify",
        data={"_csrf_token": token, "notify_enabled": "on"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("KA").notify_enabled is True

    client.post(
        "/publications/KA/notify",
        data={"_csrf_token": token, "notify_enabled": "off"},
        follow_redirects=False,
    )
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("KA").notify_enabled is False


def test_notify_toggle_requires_a_csrf_token(client: TestClient):
    _csrf_for(client)
    response = client.post("/publications/KA/notify", data={"notify_enabled": "on"})
    assert response.status_code == 400
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_publication("KA").notify_enabled is False


def test_publication_page_renders_the_notification_toggle(client: TestClient):
    page = client.get("/publications/KA")
    assert page.status_code == 200
    assert 'name="notify_enabled"' in page.text
    assert "/publications/KA/notify" in page.text


# ---------------------------------------------------------------------------
# Delisted counter in the Downloaded column (TASK-1459)
# ---------------------------------------------------------------------------


def test_publication_list_counts_issues_flipp_no_longer_lists(client: TestClient):
    """The count is per publication and comes from the grouped query.

    A publication with nothing delisted shows no parenthesis at all - an
    always-visible "(0)" would be noise on every row.
    """
    from datetime import datetime, timezone

    page = client.get("/publications")
    assert page.status_code == 200
    assert "(1)" not in page.text

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        issues = repo.list_issues(publication_id=pub.id)
        issues[0].delisted_at = datetime.now(timezone.utc)

    page = client.get("/publications")
    assert "(1)" in page.text
    assert "no longer listed by Flipp" in page.text


def test_the_delisted_count_comes_from_the_grouped_query(client: TestClient):
    """The count must not load the issues relationship (TASK-1338).

    The model has a fallback that counts by walking ``self.issues`` and
    returns the same number, so asserting the number alone passes whether
    or not the aggregate is wired up. Asserting that ``issues`` is still
    unloaded is what distinguishes the two.
    """
    from datetime import datetime, timezone

    from sqlalchemy import inspect as sa_inspect

    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        pub = repo.get_publication("KA")
        for issue in repo.list_issues(publication_id=pub.id):
            issue.delisted_at = datetime.now(timezone.utc)

    with get_session(client.app.state.session_factory) as session:
        pubs = {
            p.custom_code: p for p in DownloadRepository(session).list_publications()
        }
        assert pubs["KA"].num_delisted == 1
        assert pubs["GH"].num_delisted == 0
        assert "issues" in sa_inspect(pubs["KA"]).unloaded


# ---------------------------------------------------------------------------
# Poll now (TASK-1465)
# ---------------------------------------------------------------------------


def test_poll_now_runs_a_poll_and_reports_what_is_left(client: TestClient, monkeypatch):
    """The button has to actually poll - rendering it proves nothing."""
    kallad = {}

    def _fake_poll(client_, session_factory, output_root, workers):
        kallad["ja"] = True

    monkeypatch.setattr("flipp_dl.web.routes.poll_publications", _fake_poll)
    with get_session(client.app.state.session_factory) as session:
        DownloadRepository(session).set_setting("flipp_token", "dummy")

    token = _csrf_for(client)
    response = client.post("/settings/poll", data={"_csrf_token": token})

    assert response.status_code == 200
    assert kallad.get("ja") is True
    assert "Poll finished" in response.text
    assert "without a cover" in response.text


def test_poll_now_says_so_when_there_is_no_token(client: TestClient, monkeypatch):
    """Without a token a poll cannot do anything - say it, do not crash."""

    def _explode(*_a, **_kw):
        raise AssertionError("must not poll without a token")

    monkeypatch.setattr("flipp_dl.web.routes.poll_publications", _explode)
    monkeypatch.delenv("FLIPP_TOKEN", raising=False)

    token = _csrf_for(client)
    response = client.post("/settings/poll", data={"_csrf_token": token})

    assert response.status_code == 200
    assert "No Flipp token is configured" in response.text


def test_poll_now_requires_a_csrf_token(client: TestClient, monkeypatch):
    def _explode(*_a, **_kw):
        raise AssertionError("must not poll without a valid CSRF token")

    monkeypatch.setattr("flipp_dl.web.routes.poll_publications", _explode)
    _csrf_for(client)
    assert client.post("/settings/poll").status_code == 400


def test_the_settings_page_renders_the_poll_button(client: TestClient):
    page = client.get("/settings")
    assert 'hx-post="/settings/poll"' in page.text


# ---------------------------------------------------------------------------
# Sign in to Flipp from the settings page (TASK-1454)
# ---------------------------------------------------------------------------


def test_signing_in_stores_the_token_and_never_the_password(
    client: TestClient, monkeypatch
):
    """The password is used for one request; only the token is kept."""
    sett = {}

    def _fake_sign_in(email, password, **_kw):
        sett["email"] = email
        sett["password"] = password
        return "token-fran-flipp"

    monkeypatch.setattr("flipp_dl.web.routes.FlippClient.sign_in", _fake_sign_in)
    token = _csrf_for(client)

    response = client.post(
        "/settings/flipp-login",
        data={
            "_csrf_token": token,
            "email": "  rasmus@example.com  ",
            "password": "hemligt",
        },
    )

    assert response.status_code == 200
    assert "Signed in" in response.text
    assert sett["email"] == "rasmus@example.com"  # trimmad
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_setting("flipp_token") == "token-fran-flipp"
        # Lösenordet får inte finnas kvar någonstans i inställningarna.
        from sqlalchemy import select as sa_select

        from flipp_dl.db.models import DbSetting

        varden = [s.value for s in session.scalars(sa_select(DbSetting))]
        assert "hemligt" not in varden


def test_wrong_credentials_are_reported_without_storing_anything(
    client: TestClient, monkeypatch
):
    from flipp_dl.api import WRONG_CREDENTIALS, FlippError

    def _fake_sign_in(*_a, **_kw):
        raise FlippError(WRONG_CREDENTIALS)

    monkeypatch.setattr("flipp_dl.web.routes.FlippClient.sign_in", _fake_sign_in)
    token = _csrf_for(client)

    response = client.post(
        "/settings/flipp-login",
        data={"_csrf_token": token, "email": "fel@example.com", "password": "fel"},
    )

    assert response.status_code == 200
    assert "Wrong email address or password" in response.text
    with get_session(client.app.state.session_factory) as session:
        assert DownloadRepository(session).get_setting("flipp_token", "") == ""


def test_signing_in_requires_a_csrf_token(client: TestClient, monkeypatch):
    def _explode(*_a, **_kw):
        raise AssertionError("must not sign in without a valid CSRF token")

    monkeypatch.setattr("flipp_dl.web.routes.FlippClient.sign_in", _explode)
    _csrf_for(client)
    assert client.post("/settings/flipp-login").status_code == 400


def test_an_empty_field_does_not_reach_the_api(client: TestClient, monkeypatch):
    def _explode(*_a, **_kw):
        raise AssertionError("must not call the API without both fields")

    monkeypatch.setattr("flipp_dl.web.routes.FlippClient.sign_in", _explode)
    token = _csrf_for(client)

    response = client.post(
        "/settings/flipp-login",
        data={"_csrf_token": token, "email": "", "password": "hemligt"},
    )
    assert response.status_code == 200
    assert "Wrong email address or password" in response.text


def test_the_library_page_carries_the_import_button_and_its_csrf_token(
    client: TestClient,
):
    """The reconcile action moved here from settings (TASK-1398).

    It posts with CSRF, so the page has to render a token - the settings
    page did that for it before.
    """
    page = client.get("/library")

    assert page.status_code == 200
    assert 'hx-post="/library/import-existing"' in page.text
    assert re.search(r'_csrf_token["\s:]+[A-Za-z0-9_-]{16,}', page.text)


def test_the_settings_page_no_longer_offers_the_disk_reconcile(client: TestClient):
    page = client.get("/settings")
    assert "import-existing" not in page.text


# ---------------------------------------------------------------------------
# Test notification button (ntfy)
# ---------------------------------------------------------------------------


def test_the_test_button_sends_through_the_form_values(client: TestClient, monkeypatch):
    """Testing before saving has to work, so the form wins over the DB."""
    skickat = {}

    class _FakeChannel:
        def __init__(self, url, topic, token="", **_kw):
            skickat["url"] = url
            skickat["topic"] = topic
            skickat["token"] = token

        def send(self, title, message):
            skickat["title"] = title
            skickat["message"] = message

    monkeypatch.setattr("flipp_dl.web.routes.NtfyChannel", _FakeChannel)
    token = _csrf_for(client)

    response = client.post(
        "/settings/notify/test",
        data={
            "_csrf_token": token,
            "notify_ntfy_url": "https://ntfy.example/",
            "notify_ntfy_topic": "svc_flipp-dl",
            "notify_ntfy_token": "tk_abc",
        },
    )

    assert response.status_code == 200
    assert "Test notification sent" in response.text
    assert skickat["url"] == "https://ntfy.example/"
    assert skickat["topic"] == "svc_flipp-dl"
    assert skickat["token"] == "tk_abc"
    assert skickat["title"] == "Flipp-DL"


def test_a_blank_field_falls_back_to_what_is_saved(client: TestClient, monkeypatch):
    """The token field is never pre-filled, so blank must mean "the saved one"."""
    skickat = {}

    class _FakeChannel:
        def __init__(self, url, topic, token="", **_kw):
            skickat.update(url=url, topic=topic, token=token)

        def send(self, *_a):
            pass

    monkeypatch.setattr("flipp_dl.web.routes.NtfyChannel", _FakeChannel)
    with get_session(client.app.state.session_factory) as session:
        repo = DownloadRepository(session)
        repo.set_setting("notify_ntfy_url", "https://sparad.example")
        repo.set_setting("notify_ntfy_topic", "sparat_topic")
        repo.set_setting("notify_ntfy_token", "tk_sparad")

    token = _csrf_for(client)
    client.post("/settings/notify/test", data={"_csrf_token": token})

    assert skickat == {
        "url": "https://sparad.example",
        "topic": "sparat_topic",
        "token": "tk_sparad",
    }


def test_a_failed_delivery_is_reported_with_the_reason(client: TestClient, monkeypatch):
    from flipp_dl.notify import NotifyError

    class _FailingChannel:
        def __init__(self, *_a, **_kw):
            pass

        def send(self, *_a):
            raise NotifyError("ntfy: HTTP 403")

    monkeypatch.setattr("flipp_dl.web.routes.NtfyChannel", _FailingChannel)
    token = _csrf_for(client)

    response = client.post(
        "/settings/notify/test",
        data={
            "_csrf_token": token,
            "notify_ntfy_url": "https://ntfy.example",
            "notify_ntfy_topic": "t",
        },
    )

    assert response.status_code == 200
    assert "could not be delivered" in response.text
    assert "HTTP 403" in response.text


def test_testing_without_a_topic_says_so_instead_of_calling_out(
    client: TestClient, monkeypatch
):
    def _explode(*_a, **_kw):
        raise AssertionError("must not build a channel without url and topic")

    monkeypatch.setattr("flipp_dl.web.routes.NtfyChannel", _explode)
    token = _csrf_for(client)

    response = client.post("/settings/notify/test", data={"_csrf_token": token})

    assert response.status_code == 200
    assert "Fill in the ntfy server URL and topic" in response.text


def test_the_test_button_requires_a_csrf_token(client: TestClient, monkeypatch):
    def _explode(*_a, **_kw):
        raise AssertionError("must not send without a valid CSRF token")

    monkeypatch.setattr("flipp_dl.web.routes.NtfyChannel", _explode)
    _csrf_for(client)
    assert client.post("/settings/notify/test").status_code == 400
