"""Tests for the download-queue claiming and startup-recovery logic.

TASK-1282: queued download jobs used to get stuck forever once more than
200 newer jobs (of any type) existed, because ``_claim_next_download_job``
filtered a fixed-size ``list_jobs(limit=200)`` window in Python instead of
querying the DB directly for the oldest queued job.
"""

import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from flipp_dl import storage
from flipp_dl.db.models import DbJob, IssueStatus, JobStatus
from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session, make_session_factory
from flipp_dl.komga import KomgaError
from flipp_dl.models import Category, Issue, Publication
from flipp_dl.scheduler import (
    _claim_next_download_job,
    _is_permanent_download_error,
    build_notify_channels,
    cache_covers,
    poll_publications,
    recover_stuck_jobs,
    resolve_notify_settings_from_repo,
    run_download_queue,
    run_editions_queue,
    run_komga_sync_queue,
)


@pytest.fixture()
def session_factory():
    return make_session_factory(":memory:")


@pytest.fixture()
def session(session_factory) -> Session:
    sess = session_factory()
    yield sess
    sess.close()


@pytest.fixture()
def repo(session: Session) -> DownloadRepository:
    return DownloadRepository(session)


def _seed_issue(repo: DownloadRepository) -> int:
    """Create one publication with one issue, return the issue id."""
    pub = Publication(custom_code="KA", name="Kalle Anka & Co")
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _created = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()
    return db_issue.id


def test_claim_picks_oldest_queued_job_beyond_200_newer_jobs(repo, session_factory):
    """A bulk-queue of >200 jobs must not hide the oldest queued job.

    This reproduces TASK-1282: queue one old download job, then 250
    newer download jobs, and confirm the *oldest* one is claimed first
    even though it is far outside a 200-row "newest first" window.
    """
    issue_id = _seed_issue(repo)

    base = datetime.utcnow() - timedelta(days=1)
    oldest = DbJob(
        job_type="download",
        payload=f'{{"issue_id": {issue_id}}}',
        status=JobStatus.QUEUED,
        created_at=base,
    )
    repo.session.add(oldest)
    repo.session.commit()
    oldest_id = oldest.id

    for i in range(250):
        repo.session.add(
            DbJob(
                job_type="download",
                payload=f'{{"issue_id": {issue_id}}}',
                status=JobStatus.QUEUED,
                created_at=base + timedelta(seconds=i + 1),
            )
        )
    repo.session.commit()

    claimed = _claim_next_download_job(session_factory)
    assert claimed is not None
    job_id, _domain_pub, _domain_issue, _issue_id = claimed
    assert job_id == oldest_id


def test_recover_stuck_jobs_resets_running_download_job_and_issue(
    repo, session_factory
):
    issue_id = _seed_issue(repo)
    issue = repo.get_issue(issue_id)
    issue.status = IssueStatus.DOWNLOADING

    job = repo.create_job("download", {"issue_id": issue_id})
    repo.start_job(job.id)
    repo.session.commit()
    assert job.status == JobStatus.RUNNING

    reset_count = recover_stuck_jobs(session_factory)
    assert reset_count == 1

    refreshed_job = repo.session.get(DbJob, job.id)
    repo.session.refresh(refreshed_job)
    assert refreshed_job.status == JobStatus.QUEUED
    assert refreshed_job.started_at is None

    refreshed_issue = repo.get_issue(issue_id)
    repo.session.refresh(refreshed_issue)
    assert refreshed_issue.status == IssueStatus.QUEUED


def test_recover_stuck_jobs_leaves_other_statuses_alone(repo, session_factory):
    """Only RUNNING download jobs are touched – queued/done/poll jobs stay put."""
    issue_id = _seed_issue(repo)

    queued_job = repo.create_job("download", {"issue_id": issue_id})
    done_job = repo.create_job("download", {"issue_id": issue_id})
    repo.start_job(done_job.id)
    repo.finish_job(done_job.id)
    poll_job = repo.create_job("poll")
    repo.start_job(poll_job.id)
    repo.session.commit()

    reset_count = recover_stuck_jobs(session_factory)
    assert reset_count == 0

    assert repo.session.get(DbJob, queued_job.id).status == JobStatus.QUEUED
    assert repo.session.get(DbJob, done_job.id).status == JobStatus.DONE
    assert repo.session.get(DbJob, poll_job.id).status == JobStatus.RUNNING


def test_recover_resets_issue_stuck_without_a_job(repo, session_factory):
    """An issue queued with no job behind it must be released.

    This is the drift seen in production: the issue claimed to be
    queued while the jobs table had nothing for it, and the UI refuses
    to re-queue an issue that is already queued (TASK-1341).
    """
    issue_id = _seed_issue(repo)
    repo.mark_issue_queued(issue_id)
    repo.session.commit()

    assert recover_stuck_jobs(session_factory) == 0  # no running jobs to reset

    with get_session(session_factory) as session:
        assert DownloadRepository(session).get_issue(issue_id).status == IssueStatus.NEW


def test_recover_leaves_issues_with_a_live_job_alone(repo, session_factory):
    issue_id = _seed_issue(repo)
    repo.mark_issue_queued(issue_id)
    repo.create_job("download", {"issue_id": issue_id})
    repo.session.commit()

    recover_stuck_jobs(session_factory)

    with get_session(session_factory) as session:
        issue = DownloadRepository(session).get_issue(issue_id)
        assert issue.status == IssueStatus.QUEUED


def test_poll_does_not_backfill_issues_that_predate_watching(
    repo, session_factory, monkeypatch
):
    """TASK-1361: poll bevakar framåt - it must not sweep the back catalogue.

    The issue already existed (was discovered) before watching was turned
    on, so it is part of the back catalogue - only the explicit "Queue
    missing issues" button may queue it. Replaces the old
    test_poll_backfills_watched_publications, which asserted the opposite
    (TASK-1346 behaviour) - that is exactly the bug this task fixes.
    """
    from flipp_dl.scheduler import poll_publications

    issue_id = _seed_issue(repo)
    repo.set_watched("KA", True)
    repo.set_setting("flipp_token", "test-token")  # poll skippar utan token
    repo.session.commit()

    class FakeClient:
        def fetch_publications(self):
            return []  # nothing new discovered this poll

    poll_publications(FakeClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.NEW
        assert r.count_jobs_by_status()["queued"] == 0


def test_poll_backfills_issues_discovered_after_watching_started(repo, session_factory):
    """An issue discovered while watching, but never queued, is caught up.

    Simulates an issue that was discovered by an earlier poll tick (so
    its discovered_at is after watch_started_at) but never made it to
    QUEUED - e.g. a restart lost the in-flight job. That one must still
    be picked up; only the pre-watch back catalogue is excluded.
    """
    from datetime import timedelta

    from flipp_dl.models import Issue
    from flipp_dl.scheduler import poll_publications

    _seed_issue(repo)  # creates publication "KA" with one done-ish issue
    repo.set_watched("KA", True)
    repo.set_setting("flipp_token", "test-token")
    repo.session.commit()
    pub = repo.get_publication("KA")

    db_issue, _created = repo.upsert_issue(
        Issue(custom_code="KA-02", issue_name="Nr 2", issue_date="2024-02-01"),
        pub.id,
    )
    db_issue.discovered_at = pub.watch_started_at + timedelta(minutes=5)
    repo.session.commit()
    issue_id = db_issue.id

    class FakeClient:
        def fetch_publications(self):
            return []

    poll_publications(FakeClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.QUEUED
        assert r.count_jobs_by_status()["queued"] == 1


def test_poll_does_not_retry_failed_issues(repo, session_factory):
    """A permanently broken issue must not be re-queued every poll."""
    from flipp_dl.scheduler import poll_publications

    issue_id = _seed_issue(repo)
    repo.mark_issue_error(issue_id, "boom")
    repo.set_watched("KA", True)
    repo.set_setting("flipp_token", "test-token")  # poll skippar utan token
    repo.session.commit()

    class FakeClient:
        def fetch_publications(self):
            return []

    poll_publications(FakeClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.ERROR
        assert r.count_jobs_by_status()["queued"] == 0


def test_poll_skips_backfill_for_publication_not_yet_due(repo, session_factory):
    """A publication with its own interval is skipped until it's due (TASK-1291)."""
    from flipp_dl.scheduler import poll_publications

    issue_id = _seed_issue(repo)
    pub = repo.get_publication("KA")
    repo.set_watched("KA", True)
    repo.set_setting("flipp_token", "test-token")
    repo.set_publication_poll_interval("KA", 60)
    repo.mark_publication_poll_done(pub.id)  # just checked - not due again yet
    repo.session.commit()

    class FakeClient:
        def fetch_publications(self):
            return []

    poll_publications(FakeClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        # Backfill was skipped this tick - the issue stays NEW.
        assert r.get_issue(issue_id).status == IssueStatus.NEW
        assert r.count_jobs_by_status()["queued"] == 0


def test_poll_backfills_publication_once_its_own_interval_elapses(
    repo, session_factory
):
    """Once the override interval has passed, the publication is due again."""
    from datetime import datetime, timedelta

    from flipp_dl.scheduler import poll_publications

    issue_id = _seed_issue(repo)
    pub = repo.get_publication("KA")
    repo.set_watched("KA", True)
    # This test is about the poll-interval gate, not the back-catalogue
    # split (TASK-1361) - push discovered_at to after watch_started_at so
    # that cutoff doesn't also exclude it.
    repo.get_issue(issue_id).discovered_at = pub.watch_started_at + timedelta(minutes=1)
    repo.set_setting("flipp_token", "test-token")
    repo.set_publication_poll_interval("KA", 60)
    pub.next_poll_due_at = datetime.utcnow() - timedelta(minutes=1)
    repo.session.commit()

    class FakeClient:
        def fetch_publications(self):
            return []

    poll_publications(FakeClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.QUEUED
        assert r.count_jobs_by_status()["queued"] == 1


def test_poll_without_a_token_does_nothing(repo, session_factory):
    """No token yet: skip quietly instead of logging a failure every poll.

    The scheduler now starts even without a token so one saved in the UI
    takes effect without a restart (TASK-1342) - which means the no-token
    case is normal, not exceptional.
    """
    from flipp_dl.scheduler import poll_publications

    repo.session.commit()

    class ExplodingClient:
        token = ""

        def fetch_publications(self):
            raise AssertionError("must not call the API without a token")

    poll_publications(ExplodingClient(), session_factory, Path("/tmp"), workers=1)

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.count_jobs_by_status() == {
            "queued": 0,
            "running": 0,
            "done": 0,
            "error": 0,
        }


# ---------------------------------------------------------------------------
# Komga sync (TASK-1326)
# ---------------------------------------------------------------------------


def _one_page_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakeFlippClient:
    """Minimal FlippClient stand-in so run_download_queue can succeed."""

    token = "dummy-token"

    def __init__(self) -> None:
        self._pdf = _one_page_pdf()

    def fetch_issue_pdf_urls(self, _pub_code, _issue_code):
        return ["http://example.invalid/page0.pdf"]

    def download_pdf(self, _url):
        return self._pdf


class _FakeKomgaClient:
    """Records scan_library calls; monkeypatched in for KomgaClient.

    Also fakes the level-2 (TASK-1327) metadata-push surface. The two
    class-level dicts are the fake's canned "server state" - set them
    before calling ``run_komga_sync_queue`` and reset them (and
    ``instances``) at the start of each test that uses them.
    """

    instances: list["_FakeKomgaClient"] = []
    series_by_name: dict[str, dict] = {}
    books_by_series: dict[object, list[dict]] = {}

    def __init__(self, url, *, username="", password="", api_key=""):
        self.url = url
        self.username = username
        self.password = password
        self.api_key = api_key
        self.scanned: list[str] = []
        self.patched_series: list[tuple] = []
        self.patched_books: list[tuple] = []
        self.uploaded_thumbnails: list[tuple] = []
        _FakeKomgaClient.instances.append(self)

    def scan_library(self, library_id):
        self.scanned.append(library_id)

    def find_series_by_name(self, _library_id, name):
        return _FakeKomgaClient.series_by_name.get(name)

    def patch_series_metadata(self, series_id, **fields):
        self.patched_series.append((series_id, fields))

    def list_series_books(self, series_id):
        return _FakeKomgaClient.books_by_series.get(series_id, [])

    def find_book_by_stems(self, series_id, stems):
        for book in self.list_series_books(series_id):
            if book.get("name") in stems:
                return book
        return None

    def patch_book_metadata(self, book_id, **fields):
        self.patched_books.append((book_id, fields))

    def upload_series_thumbnail(self, series_id, content, filename):
        self.uploaded_thumbnails.append((series_id, content, filename))


class _FailingKomgaClient:
    def __init__(self, url, *, username="", password="", api_key=""):
        pass

    def scan_library(self, library_id):
        raise KomgaError("Komga is down")


def _queue_download_job(repo: DownloadRepository) -> tuple[int, int]:
    """Seed an issue and a queued download job for it. Returns (issue_id, job_id)."""
    repo.set_setting("flipp_token", "dummy-token")
    issue_id = _seed_issue(repo)
    job = repo.create_job("download", {"issue_id": issue_id})
    repo.session.commit()
    return issue_id, job.id


def test_run_download_queue_queues_komga_sync_when_enabled(
    repo, session_factory, tmp_path
):
    issue_id, _job_id = _queue_download_job(repo)
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    repo.session.commit()

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.DONE

        komga_job = r.get_oldest_queued_job("komga_sync")
        assert komga_job is not None
        payload = json.loads(komga_job.payload)
        assert payload["library_id"] == "lib-1"
        assert payload["issue_id"] == issue_id


def test_run_download_queue_does_not_queue_komga_sync_when_disabled(
    repo, session_factory, tmp_path
):
    """Default is off - a successful download must not touch Komga at all."""
    _queue_download_job(repo)
    repo.session.commit()

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_oldest_queued_job("komga_sync") is None
        assert r.count_jobs_by_status()["queued"] == 0


def test_run_download_queue_does_not_queue_komga_sync_without_library_id(
    repo, session_factory, tmp_path
):
    """Enabled but not fully configured yet must not produce a failed job."""
    _queue_download_job(repo)
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    # no komga_library_id saved
    repo.session.commit()

    run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_oldest_queued_job("komga_sync") is None
        assert r.count_jobs_by_status()["error"] == 0


def test_run_komga_sync_queue_scans_library_and_finishes_job(
    repo, session_factory, monkeypatch
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    job = repo.create_job("komga_sync", {"library_id": "lib-1"})
    repo.session.commit()
    job_id = job.id

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    assert _FakeKomgaClient.instances[0].scanned == ["lib-1"]
    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        finished = r.get_job(job_id)
        assert finished.status == JobStatus.DONE


def test_run_komga_sync_queue_is_noop_when_disabled(repo, session_factory, monkeypatch):
    """KOMGA_ENABLED off: no HTTP call, queued jobs are left untouched."""

    def _explode(*_a, **_kw):
        raise AssertionError("must not construct a KomgaClient while disabled")

    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _explode)

    job = repo.create_job("komga_sync", {"library_id": "lib-1"})
    repo.session.commit()
    job_id = job.id

    processed = run_komga_sync_queue(session_factory)
    assert processed == 0

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        untouched = r.get_job(job_id)
        assert untouched.status == JobStatus.QUEUED


def test_komga_failure_marks_job_error_but_issue_stays_done(
    repo, session_factory, tmp_path, monkeypatch
):
    """A Komga outage must never flip a successfully downloaded issue back."""
    issue_id, _job_id = _queue_download_job(repo)
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    repo.session.commit()

    run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )

    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FailingKomgaClient)
    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        # The issue that finished downloading successfully must still be
        # "done" even though the follow-up Komga scan failed.
        assert r.get_issue(issue_id).status == IssueStatus.DONE

        komga_jobs = r.list_jobs(job_type="komga_sync")
        assert len(komga_jobs) == 1
        assert komga_jobs[0].status == JobStatus.ERROR
        assert komga_jobs[0].error_message == "Komga is down"


def test_komga_failure_notifies_once_and_recovery_notifies(
    repo, session_factory, monkeypatch
):
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    repo.session.commit()

    def queue_job():
        repo.create_job("komga_sync", {"library_id": "lib-1"})
        repo.session.commit()

    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FailingKomgaClient)
    queue_job()
    run_komga_sync_queue(session_factory, max_jobs=1)
    assert len(channel.sent) == 1
    assert "misslyckades" in channel.sent[0][0]

    queue_job()
    run_komga_sync_queue(session_factory, max_jobs=1)
    assert len(channel.sent) == 1

    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()
    queue_job()
    run_komga_sync_queue(session_factory, max_jobs=1)
    assert len(channel.sent) == 2
    assert "fungerar igen" in channel.sent[1][0]


class _FailingEditionsClient:
    def fetch_editions(self, _code):
        raise RuntimeError("PageSuite is down")


class _SuccessfulEditionsClient:
    def fetch_editions(self, _code):
        return []


def test_editions_failure_notifies_once_and_recovery_notifies(
    repo, session_factory, monkeypatch
):
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    _seed_issue(repo)

    def queue_job():
        repo.create_job("discover_editions")
        repo.session.commit()

    queue_job()
    run_editions_queue(session_factory, client=_FailingEditionsClient())
    assert len(channel.sent) == 1
    assert "misslyckades" in channel.sent[0][0]

    queue_job()
    run_editions_queue(session_factory, client=_FailingEditionsClient())
    assert len(channel.sent) == 1

    queue_job()
    run_editions_queue(session_factory, client=_SuccessfulEditionsClient())
    assert len(channel.sent) == 2
    assert "fungerar igen" in channel.sent[1][0]


# ---------------------------------------------------------------------------
# Issue cover backfill (TASK-1374)
# ---------------------------------------------------------------------------


class _FakeCoverFetchResponse:
    def __init__(self, content: bytes = b"\xff\xd8fake-jpeg"):
        self.content = content
        self.headers = {"Content-Type": "image/jpeg"}
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeCoverFetchSession:
    """Stand-in for requests.Session - always "succeeds" with a fake image."""

    def __init__(self):
        self.requested_urls: list[str] = []

    def get(self, url, timeout=None):
        self.requested_urls.append(url)
        return _FakeCoverFetchResponse()


def _seed_issues_without_cover(repo: DownloadRepository, count: int) -> list[int]:
    pub = Publication(custom_code="KA", name="Kalle Anka & Co")
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    ids = []
    for i in range(count):
        issue = Issue(
            custom_code=f"KA-{i:02d}", issue_name=f"Nr {i}", issue_date="2024-01-01"
        )
        db_issue, _created = repo.upsert_issue(issue, db_pub.id)
        ids.append(db_issue.id)
    repo.session.commit()
    return ids


def test_cache_covers_backfills_issues_missing_from_before_the_cache_existed(
    repo, session_factory, monkeypatch, tmp_path
):
    """Issues that predate the cover cache (TASK-1345) get filled in too,
    not just newly-discovered ones (TASK-1374)."""
    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    fake_session = _FakeCoverFetchSession()
    monkeypatch.setattr("flipp_dl.scheduler.build_session", lambda: fake_session)

    issue_ids = _seed_issues_without_cover(repo, 3)
    repo.session.commit()

    cached = cache_covers(session_factory)

    assert cached == 3
    for issue_id in issue_ids:
        assert repo.get_issue(issue_id).cover_cache_path is not None


def test_cache_covers_backfill_is_capped_per_poll_tick(
    repo, session_factory, monkeypatch, tmp_path
):
    """17660+ back-catalogue issues must not turn into one giant burst of
    external requests on a single poll tick (TASK-1374)."""
    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    monkeypatch.setattr("flipp_dl.scheduler.ISSUE_COVER_BACKFILL_PER_POLL", 2)
    fake_session = _FakeCoverFetchSession()
    monkeypatch.setattr("flipp_dl.scheduler.build_session", lambda: fake_session)

    _seed_issues_without_cover(repo, 5)

    cached = cache_covers(session_factory)

    assert cached == 2
    assert len(fake_session.requested_urls) == 2
    still_missing = repo.issues_needing_cover_backfill(limit=10)
    assert len(still_missing) == 3


def test_cache_covers_backfill_skips_issues_already_cached(
    repo, session_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    fake_session = _FakeCoverFetchSession()
    monkeypatch.setattr("flipp_dl.scheduler.build_session", lambda: fake_session)

    issue_ids = _seed_issues_without_cover(repo, 2)
    repo.set_issue_cover_cache(issue_ids[0], "issue-KA-00.jpg")
    repo.session.commit()

    cached = cache_covers(session_factory)

    assert cached == 1
    assert len(fake_session.requested_urls) == 1
    assert "eid=KA-01" in fake_session.requested_urls[0]


# ---------------------------------------------------------------------------
# Metadata + cover push (TASK-1327)
# ---------------------------------------------------------------------------


def _seed_mapped_issue(
    repo: DownloadRepository,
    *,
    description: str | None = None,
    categories: list[Category] | None = None,
    cover_cache_path: str | None = None,
) -> tuple[int, str]:
    """Seed a publication + issue, return (issue_id, expected book stem)."""
    pub = Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        description=description,
        categories=categories or [],
    )
    db_pub = repo.upsert_publication(pub)
    if cover_cache_path:
        db_pub.cover_cache_path = cover_cache_path
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 12", issue_date="2024-03-01")
    db_issue, _created = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()
    stem = Path(storage.issue_filename(pub, issue)).stem
    return db_issue.id, stem


def _queue_komga_sync_job(repo: DownloadRepository, issue_id: int) -> int:
    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    job = repo.create_job("komga_sync", {"library_id": "lib-1", "issue_id": issue_id})
    repo.session.commit()
    return job.id


def test_komga_sync_maps_series_and_pushes_metadata(repo, session_factory, monkeypatch):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }

    issue_id, stem = _seed_mapped_issue(
        repo,
        description="<p>Kul serie</p>",
        categories=[Category(id=1, name="Humor")],
    )
    _FakeKomgaClient.books_by_series = {55: [{"id": 77, "name": stem}]}
    job_id = _queue_komga_sync_job(repo, issue_id)

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.DONE
        assert r.get_publication("KA").komga_series_id == 55

    client = _FakeKomgaClient.instances[0]
    assert client.scanned == ["lib-1"]
    series_id, series_fields = client.patched_series[0]
    assert series_id == 55
    assert series_fields["title"] == "Kalle Anka & Co"
    assert series_fields["summary"] == "Kul serie"
    assert series_fields["publisher"] == "Egmont"
    assert series_fields["language"] == "sv"
    assert series_fields["genres"] == ["Humor"]

    book_id, book_fields = client.patched_books[0]
    assert book_id == 77
    assert book_fields["title"] == "Nr 12"
    assert book_fields["number"] == "12"
    assert book_fields["numberSort"] == 12.0
    assert book_fields["releaseDate"] == "2024-03-01"


def test_komga_sync_caches_series_id_and_never_looks_up_again(
    repo, session_factory, monkeypatch
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }

    issue_id, stem = _seed_mapped_issue(repo)
    _FakeKomgaClient.books_by_series = {55: [{"id": 77, "name": stem}]}
    _queue_komga_sync_job(repo, issue_id)
    run_komga_sync_queue(session_factory, max_jobs=1)

    # Second sync: even if the fake "forgot" the series, the cached id on
    # the publication must be used instead of searching again.
    _FakeKomgaClient.series_by_name = {}
    _queue_komga_sync_job(repo, issue_id)
    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        jobs = r.list_jobs(job_type="komga_sync")
        assert all(j.status == JobStatus.DONE for j in jobs)
        assert r.get_publication("KA").komga_series_id == 55


def test_komga_sync_without_series_match_finishes_job_and_stays_unmapped(
    repo, session_factory, monkeypatch
):
    """No exact folder-name match yet - not an error, just "try next time"."""
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {}
    _FakeKomgaClient.books_by_series = {}

    issue_id, _stem = _seed_mapped_issue(repo)
    job_id = _queue_komga_sync_job(repo, issue_id)

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        job = r.get_job(job_id)
        assert job.status == JobStatus.DONE
        assert job.error_message is None
        assert r.get_publication("KA").komga_series_id is None


def test_komga_sync_book_not_found_times_out_with_job_error(
    repo, session_factory, monkeypatch
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    monkeypatch.setenv("KOMGA_WAIT_SECONDS", "0")
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }
    _FakeKomgaClient.books_by_series = {55: []}  # scan hasn't picked it up yet

    issue_id, _stem = _seed_mapped_issue(repo)
    job_id = _queue_komga_sync_job(repo, issue_id)

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        job = r.get_job(job_id)
        assert job.status == JobStatus.ERROR
        assert "not found" in job.error_message.lower()
        # The series mapping itself must still have been cached, even
        # though the book lookup afterwards timed out.
        assert r.get_publication("KA").komga_series_id == 55


def test_komga_sync_uploads_cached_cover_as_thumbnail(
    repo, session_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }

    (tmp_path / "pub-KA.jpg").write_bytes(b"\xff\xd8fake-jpeg")
    issue_id, stem = _seed_mapped_issue(repo, cover_cache_path="pub-KA.jpg")
    _FakeKomgaClient.books_by_series = {55: [{"id": 77, "name": stem}]}
    _queue_komga_sync_job(repo, issue_id)

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    client = _FakeKomgaClient.instances[0]
    assert len(client.uploaded_thumbnails) == 1
    series_id, content, filename = client.uploaded_thumbnails[0]
    assert series_id == 55
    assert content == b"\xff\xd8fake-jpeg"
    assert filename == "pub-KA.jpg"


def test_komga_sync_skips_cover_upload_when_disabled(
    repo, session_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    monkeypatch.setenv("KOMGA_PUSH_COVER", "false")
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }

    (tmp_path / "pub-KA.jpg").write_bytes(b"\xff\xd8fake-jpeg")
    issue_id, stem = _seed_mapped_issue(repo, cover_cache_path="pub-KA.jpg")
    _FakeKomgaClient.books_by_series = {55: [{"id": 77, "name": stem}]}
    _queue_komga_sync_job(repo, issue_id)

    run_komga_sync_queue(session_factory, max_jobs=1)

    client = _FakeKomgaClient.instances[0]
    assert client.uploaded_thumbnails == []


def test_komga_sync_matches_disambiguated_filename(repo, session_factory, monkeypatch):
    """Bok-uppslaget måste tåla filnamn med "(kortkod)"-suffix (TASK-1349)."""
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {
        "Kalle Anka och Co": {"id": 55, "name": "Kalle Anka och Co"}
    }

    issue_id, stem = _seed_mapped_issue(repo)
    disambiguated_name = f"{stem} (KA-01)"
    _FakeKomgaClient.books_by_series = {55: [{"id": 99, "name": disambiguated_name}]}
    job_id = _queue_komga_sync_job(repo, issue_id)

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.DONE

    client = _FakeKomgaClient.instances[0]
    assert client.patched_books[0][0] == 99


def test_komga_sync_without_issue_id_still_scans_library(
    repo, session_factory, monkeypatch
):
    """A komga_sync job without issue_id (e.g. from an older payload shape)
    must still trigger the scan and finish cleanly - it just can't push
    metadata for a specific issue."""
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    job = repo.create_job("komga_sync", {"library_id": "lib-1"})
    repo.session.commit()
    job_id = job.id

    processed = run_komga_sync_queue(session_factory, max_jobs=1)
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.DONE

    assert _FakeKomgaClient.instances[0].scanned == ["lib-1"]


# ---------------------------------------------------------------------------
# Notifications (TASK-1293)
# ---------------------------------------------------------------------------


class _FailingFlippClient(_FakeFlippClient):
    """Same as _FakeFlippClient, but every download raises - error path.

    Raises a 404 (permanent, TASK-1363) rather than a generic exception
    so this stays a same-tick failure: notification bundling is tested
    here, not the automatic-retry path (covered separately in
    ``test_run_download_queue_schedules_retry_for_transient_error``).
    """

    def fetch_issue_pdf_urls(self, _pub_code, _issue_code):
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError("Not Found", response=response)


class _RecordingChannel:
    name = "recording"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, title, message):
        self.sent.append((title, message))


class _ExplodingChannel:
    """A channel whose send() always raises - must never break a job."""

    name = "exploding"

    def send(self, title, message):
        raise RuntimeError("channel is down")


def test_resolve_notify_settings_from_repo_reads_saved_values(repo, monkeypatch):
    # This machine has NTFY_URL/NTFY_ENABLED/etc. set in the real shell
    # environment for unrelated services - env overrides DbSetting (same
    # precedence as Komga), so isolate from it to test the DB-only path.
    for env_key in (
        "NTFY_ENABLED",
        "NTFY_URL",
        "NTFY_TOPIC",
        "NTFY_TOKEN",
        "NOTIFY_WEBHOOK_ENABLED",
        "NOTIFY_WEBHOOK_URL",
    ):
        monkeypatch.delenv(env_key, raising=False)
    repo.set_setting("notify_ntfy_enabled", "true")
    repo.set_setting("notify_ntfy_url", "https://ntfy.example.com")
    repo.set_setting("notify_ntfy_topic", "flipp-dl")
    repo.set_setting("notify_ntfy_token", "secret")
    repo.set_setting("notify_webhook_enabled", "true")
    repo.set_setting("notify_webhook_url", "https://hooks.example.com/x")
    repo.session.commit()

    settings = resolve_notify_settings_from_repo(repo)

    assert settings == {
        "ntfy_enabled": True,
        "ntfy_url": "https://ntfy.example.com",
        "ntfy_topic": "flipp-dl",
        "ntfy_token": "secret",
        "webhook_enabled": True,
        "webhook_url": "https://hooks.example.com/x",
    }


def test_resolve_notify_settings_defaults_ntfy_url_to_public_server(repo, monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    settings = resolve_notify_settings_from_repo(repo)
    assert settings["ntfy_url"] == "https://ntfy.sh"
    assert settings["ntfy_enabled"] is False
    assert settings["webhook_enabled"] is False


def test_resolve_notify_settings_env_overrides_db(repo, monkeypatch):
    repo.set_setting("notify_ntfy_topic", "db-topic")
    repo.session.commit()
    monkeypatch.setenv("NTFY_TOPIC", "env-topic")

    settings = resolve_notify_settings_from_repo(repo)

    assert settings["ntfy_topic"] == "env-topic"


def test_build_notify_channels_respects_independent_enabled_flags():
    """Either channel can be on alone, both, or neither - never coupled."""
    base = {
        "ntfy_enabled": False,
        "ntfy_url": "https://ntfy.sh",
        "ntfy_topic": "flipp-dl",
        "ntfy_token": "",
        "webhook_enabled": False,
        "webhook_url": "https://hooks.example.com/x",
    }

    assert build_notify_channels(base) == []
    assert [c.name for c in build_notify_channels({**base, "ntfy_enabled": True})] == [
        "ntfy"
    ]
    assert [
        c.name for c in build_notify_channels({**base, "webhook_enabled": True})
    ] == ["webhook"]
    assert [
        c.name
        for c in build_notify_channels(
            {**base, "ntfy_enabled": True, "webhook_enabled": True}
        )
    ] == ["ntfy", "webhook"]


def test_build_notify_channels_skips_enabled_channel_missing_required_field():
    """Enabled-but-unconfigured must not build a channel that would crash."""
    settings = {
        "ntfy_enabled": True,
        "ntfy_url": "https://ntfy.sh",
        "ntfy_topic": "",  # missing
        "ntfy_token": "",
        "webhook_enabled": True,
        "webhook_url": "",  # missing
    }
    assert build_notify_channels(settings) == []


def _enable_ntfy(repo: DownloadRepository) -> None:
    repo.set_setting("notify_ntfy_enabled", "true")
    repo.set_setting("notify_ntfy_topic", "flipp-dl")
    repo.session.commit()


class _FailingPollClient:
    token = ""

    def fetch_publications(self):
        from flipp_dl.api import FlippError

        raise FlippError("Token expired")


class _SuccessfulPollClient:
    token = ""

    def fetch_publications(self):
        return []


def test_poll_failure_notifies_once_and_recovery_notifies(
    repo, session_factory, tmp_path, monkeypatch
):
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    monkeypatch.setattr("flipp_dl.scheduler.cache_covers", lambda *_args: 0)
    _enable_ntfy(repo)
    repo.set_setting("flipp_token", "expired-token")
    repo.session.commit()

    poll_publications(_FailingPollClient(), session_factory, tmp_path)
    assert len(channel.sent) == 1
    assert "misslyckades" in channel.sent[0][0]

    poll_publications(_FailingPollClient(), session_factory, tmp_path)
    assert len(channel.sent) == 1

    poll_publications(_SuccessfulPollClient(), session_factory, tmp_path)
    assert len(channel.sent) == 2
    assert "fungerar igen" in channel.sent[1][0]


def test_poll_failure_without_channels_is_silent_and_does_not_crash(
    repo, session_factory, tmp_path
):
    repo.set_setting("flipp_token", "expired-token")
    repo.session.commit()

    poll_publications(_FailingPollClient(), session_factory, tmp_path)

    with get_session(session_factory) as session:
        assert DownloadRepository(session).get_setting("notify_failure_poll") == "true"


def test_run_download_queue_sends_one_bundled_notification_for_several_issues(
    repo, session_factory, tmp_path, monkeypatch
):
    """A drain that processes several issues sends ONE summary notification,
    not one per issue - the point being a bulk backfill of thousands of
    issues must not flood the channel (see TASK-1293's implementation
    hints)."""
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)

    repo.set_setting("flipp_token", "dummy-token")
    issue_ids = []
    for i in range(3):
        pub = Publication(custom_code="KA", name="Kalle Anka & Co")
        db_pub = repo.upsert_publication(pub)
        repo.set_publication_notify("KA", True)
        repo.session.commit()
        issue = Issue(
            custom_code=f"KA-{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
        )
        db_issue, _created = repo.upsert_issue(issue, db_pub.id)
        repo.session.commit()
        repo.create_job("download", {"issue_id": db_issue.id})
        repo.session.commit()
        issue_ids.append(db_issue.id)

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1
    )
    assert processed == 3

    # Exactly one notification for all three successful downloads, not three.
    assert len(channel.sent) == 1
    title, message = channel.sent[0]
    assert "3" in title
    for issue_id_count in range(3):
        assert f"Nr {issue_id_count}" in message


def test_run_download_queue_sends_no_notification_when_no_channel_configured(
    repo, session_factory, tmp_path
):
    """Default (no channel enabled) must not touch the network at all -
    build_notify_channels returning [] is enough, no monkeypatch needed."""
    _queue_download_job(repo)
    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )
    assert processed == 1  # succeeds without a channel configured


def test_run_download_queue_notification_failure_does_not_affect_job_status(
    repo, session_factory, tmp_path, monkeypatch
):
    """A channel that raises must never fail the download job - the
    notification step runs strictly after the job/issue status is already
    committed as done."""
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels",
        lambda settings: [_ExplodingChannel()],
    )
    _enable_ntfy(repo)
    issue_id, job_id = _queue_download_job(repo)

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_issue(issue_id).status == IssueStatus.DONE
        assert r.get_job(job_id).status == JobStatus.DONE


def test_run_download_queue_bundles_failures_into_their_own_notification(
    repo, session_factory, tmp_path, monkeypatch
):
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    issue_id, job_id = _queue_download_job(repo)
    repo.set_publication_notify("KA", True)
    repo.session.commit()

    processed = run_download_queue(
        _FailingFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.ERROR

    assert len(channel.sent) == 1
    title, _message = channel.sent[0]
    assert "misslyckades" in title


# ---------------------------------------------------------------------------
# Automatic retry (TASK-1363)
# ---------------------------------------------------------------------------


class _TransientFailingFlippClient(_FakeFlippClient):
    """Every download raises a network-style error - retryable."""

    def fetch_issue_pdf_urls(self, _pub_code, _issue_code):
        raise requests.ConnectionError("Connection refused")


def test_is_permanent_download_error_classifies_by_status_code():
    def _http_error(status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(f"{status}", response=response)

    assert _is_permanent_download_error(_http_error(404)) is True
    assert _is_permanent_download_error(_http_error(403)) is True
    assert _is_permanent_download_error(_http_error(500)) is False
    assert _is_permanent_download_error(_http_error(503)) is False
    assert _is_permanent_download_error(requests.ConnectionError("boom")) is False
    assert _is_permanent_download_error(requests.Timeout("boom")) is False
    assert _is_permanent_download_error(RuntimeError("database is locked")) is False

    # FlippError re-raises the HTTPError via `from exc` - the status
    # code must still be reachable through __cause__.
    from flipp_dl.api import FlippError

    try:
        try:
            raise _http_error(404)
        except requests.HTTPError as inner:
            raise FlippError("wrapped") from inner
    except FlippError as wrapped:
        assert _is_permanent_download_error(wrapped) is True


def test_run_download_queue_schedules_retry_for_transient_error(
    repo, session_factory, tmp_path
):
    issue_id, job_id = _queue_download_job(repo)

    processed = run_download_queue(
        _TransientFailingFlippClient(),
        session_factory,
        tmp_path / "out",
        workers=1,
        max_jobs=1,
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.ERROR
        issue = r.get_issue(issue_id)
        assert issue.status == IssueStatus.RETRY_PENDING
        assert issue.retry_count == 1
        assert issue.next_retry_at is not None

    # No new download job exists yet - the retry is not eligible until
    # its own backoff window elapses.
    assert _claim_next_download_job(session_factory) is None


def test_retryable_download_failure_sends_no_notification(
    repo, session_factory, tmp_path, monkeypatch
):
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    issue_id, _job_id = _queue_download_job(repo)
    publication = repo.get_issue(issue_id).publication
    repo.set_publication_notify(publication.custom_code, True)
    repo.session.commit()

    processed = run_download_queue(
        _TransientFailingFlippClient(),
        session_factory,
        tmp_path / "out",
        workers=1,
        max_jobs=1,
    )

    assert processed == 1
    assert channel.sent == []


def test_run_download_queue_gives_up_after_max_auto_retries(
    repo, session_factory, tmp_path
):
    """A transient-looking failure still stops retrying eventually and
    lands on ERROR exactly like a permanent one - it just took the long
    way round via three growing-delay attempts first."""
    from flipp_dl.db.repository import MAX_AUTO_RETRIES

    issue_id, job_id = _queue_download_job(repo)

    # MAX_AUTO_RETRIES failures use up the automatic-retry budget; the
    # extra +1 is the one that finally gives up.
    for _ in range(MAX_AUTO_RETRIES + 1):
        with get_session(session_factory) as session:
            r = DownloadRepository(session)
            issue = r.get_issue(issue_id)
            # Force the backoff window into the past so the next tick
            # picks it straight back up instead of waiting minutes.
            if issue.next_retry_at is not None:
                issue.next_retry_at = datetime.utcnow() - timedelta(minutes=1)

        processed = run_download_queue(
            _TransientFailingFlippClient(),
            session_factory,
            tmp_path / "out",
            workers=1,
            max_jobs=1,
        )
        assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        issue = r.get_issue(issue_id)
        assert issue.status == IssueStatus.ERROR
        assert issue.retry_count == MAX_AUTO_RETRIES

    # Still no queued job left behind, and the exhausted issue does not
    # auto-requeue itself again.
    assert _claim_next_download_job(session_factory) is None
    with get_session(session_factory) as session:
        assert DownloadRepository(session).requeue_due_retries() == 0


def test_run_download_queue_does_not_retry_permanent_error(
    repo, session_factory, tmp_path
):
    issue_id, job_id = _queue_download_job(repo)

    processed = run_download_queue(
        _FailingFlippClient(),
        session_factory,
        tmp_path / "out",
        workers=1,
        max_jobs=1,
    )
    assert processed == 1

    with get_session(session_factory) as session:
        r = DownloadRepository(session)
        assert r.get_job(job_id).status == JobStatus.ERROR
        issue = r.get_issue(issue_id)
        assert issue.status == IssueStatus.ERROR
        assert issue.retry_count == 0
        assert issue.next_retry_at is None


def test_cover_fetching_does_not_hold_the_write_lock(tmp_path, monkeypatch):
    """Another writer must get through while covers are being fetched.

    The whole poll used to run in one transaction with the cover fetch
    inside it, so the write lock was held across thousands of HTTP
    requests. Every other writer - the disk import, a manual download,
    a cancel - waited out the 30 s lock timeout and failed with
    "database is locked" (TASK-1391).
    """
    from flipp_dl.scheduler import cache_covers

    factory = make_session_factory(tmp_path / "flipp.db")
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(Publication(custom_code="KA", name="Kalle"))
        for i in range(3):
            repo.upsert_issue(
                Issue(
                    custom_code=f"ka{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
                ),
                db_pub.id,
            )

    monkeypatch.setattr("flipp_dl.scheduler.default_cover_cache_root", lambda: tmp_path)
    wrote_during_fetch = []

    class LockProbingSession:
        """Stands in for the HTTP session; writes from a second session
        at the moment a cover would be fetched."""

        def get(self, url, **kwargs):
            with get_session(factory) as other:
                DownloadRepository(other).set_setting("probe", "written")
            wrote_during_fetch.append(url)

            class Resp:
                status_code = 200
                content = b"\xff\xd8\xff\xe0jpeg"
                headers = {"content-type": "image/jpeg"}

                def raise_for_status(self):
                    return None

            return Resp()

    monkeypatch.setattr(
        "flipp_dl.scheduler.build_session", lambda: LockProbingSession()
    )

    cache_covers(factory)

    assert wrote_during_fetch, "no cover was fetched - the test proves nothing"
    with get_session(factory) as session:
        assert DownloadRepository(session).get_setting("probe") == "written"


def test_run_download_queue_uses_saved_secondary_output_root(
    repo, session_factory, tmp_path
):
    issue_id, _job_id = _queue_download_job(repo)
    secondary_root = tmp_path / "secondary"
    repo.set_setting("secondary_output_root", str(secondary_root))
    publication = repo.get_issue(issue_id).publication
    publication.destination = "secondary"
    repo.session.commit()

    processed = run_download_queue(
        _FakeFlippClient(),
        session_factory,
        tmp_path / "primary",
        workers=1,
        max_jobs=1,
    )

    assert processed == 1
    with get_session(session_factory) as session:
        issue = DownloadRepository(session).get_issue(issue_id)
        assert Path(issue.file_path).is_relative_to(secondary_root)
        assert Path(issue.file_path).is_file()


def test_a_silent_publication_sends_no_notification(
    repo, session_factory, tmp_path, monkeypatch
):
    """Opt-in per publication: a configured channel is not enough.

    Without this the flag would look like it worked (it is stored, the UI
    shows it) while every download still notified.
    """
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    _queue_download_job(repo)  # publikationen KA lämnas tyst

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1, max_jobs=1
    )

    assert processed == 1
    assert channel.sent == []


def test_only_the_opted_in_publication_is_listed_in_the_notification(
    repo, session_factory, tmp_path, monkeypatch
):
    """A drain covering two publications announces only the one opted in."""
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)
    repo.set_setting("flipp_token", "dummy-token")

    for code, namn, tyst in (("KA", "Kalle Anka & Co", False), ("BI", "Bilar", True)):
        db_pub = repo.upsert_publication(Publication(custom_code=code, name=namn))
        repo.set_publication_notify(code, not tyst)
        repo.session.commit()
        db_issue, _ = repo.upsert_issue(
            Issue(
                custom_code=f"{code}-1",
                issue_name=f"Nr 1 {namn}",
                issue_date="2024-01-01",
            ),
            db_pub.id,
        )
        repo.session.commit()
        repo.create_job("download", {"issue_id": db_issue.id})
        repo.session.commit()

    processed = run_download_queue(
        _FakeFlippClient(), session_factory, tmp_path / "out", workers=1
    )

    assert processed == 2
    assert len(channel.sent) == 1
    _title, message = channel.sent[0]
    assert "Kalle Anka & Co" in message
    assert "Bilar" not in message


# ---------------------------------------------------------------------------
# One scan per drain, not per job (TASK-1458)
# ---------------------------------------------------------------------------


def test_a_drain_scans_the_library_once_no_matter_how_many_jobs(
    repo, session_factory, monkeypatch
):
    """A back-catalogue fetch queues hundreds of jobs for one library.

    Scanning per job made that hundreds of scans, each followed by its own
    wait - so the tick spent hours doing what one scan covers.
    """
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    for _ in range(5):
        repo.create_job("komga_sync", {"library_id": "lib-1"})
    repo.session.commit()

    processed = run_komga_sync_queue(session_factory)

    assert processed == 5
    scans = [lib for i in _FakeKomgaClient.instances for lib in i.scanned]
    assert scans == ["lib-1"]


def test_two_libraries_in_one_drain_are_scanned_once_each(
    repo, session_factory, monkeypatch
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _FakeKomgaClient)
    _FakeKomgaClient.instances.clear()

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    for lib in ("lib-1", "lib-2", "lib-1", "lib-2"):
        repo.create_job("komga_sync", {"library_id": lib})
    repo.session.commit()

    run_komga_sync_queue(session_factory)

    scans = [lib for i in _FakeKomgaClient.instances for lib in i.scanned]
    assert sorted(scans) == ["lib-1", "lib-2"]


class _SlowIndexKomgaClient(_FakeKomgaClient):
    """Komga that only reveals the book after a second scan.

    Mirrors the real race: the scan is asynchronous, so the first job of a
    drain can look for a book that is on disk but not indexed yet.
    """

    scans_before_book = 2

    def list_series_books(self, series_id):
        total = sum(len(i.scanned) for i in _FakeKomgaClient.instances)
        if total < _SlowIndexKomgaClient.scans_before_book:
            return []
        return _FakeKomgaClient.books_by_series.get(series_id, [])


def test_a_book_not_indexed_yet_gets_one_more_scan_before_failing(
    repo, session_factory, monkeypatch, tmp_path
):
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _SlowIndexKomgaClient)
    monkeypatch.setenv("KOMGA_WAIT_SECONDS", "0")
    _FakeKomgaClient.instances.clear()
    _FakeKomgaClient.series_by_name = {"Kalle Anka och Co": {"id": "s-1"}}

    issue_id = _seed_issue(repo)
    # Komga names the book after the file, and the file name comes from
    # storage.issue_filename - not from the raw publication name (safe_name
    # turns "&" into "och").
    from flipp_dl import storage

    stem = Path(
        storage.issue_filename(
            Publication(custom_code="KA", name="Kalle Anka & Co"),
            Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01"),
        )
    ).stem
    with get_session(session_factory) as session:
        pdf = tmp_path / f"{stem}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        DownloadRepository(session).mark_issue_done(issue_id, str(pdf))
    _FakeKomgaClient.books_by_series = {"s-1": [{"id": "b-1", "name": stem}]}

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    job = repo.create_job("komga_sync", {"library_id": "lib-1", "issue_id": issue_id})
    repo.session.commit()
    job_id = job.id

    run_komga_sync_queue(session_factory)

    scans = [lib for i in _FakeKomgaClient.instances for lib in i.scanned]
    assert scans == ["lib-1", "lib-1"]  # first scan, then the retry
    with get_session(session_factory) as session:
        assert DownloadRepository(session).get_job(job_id).status == JobStatus.DONE
    _FakeKomgaClient.series_by_name = {}
    _FakeKomgaClient.books_by_series = {}


def test_a_komga_drain_notifies_once_even_when_jobs_alternate(
    repo, session_factory, monkeypatch
):
    """Per-job notification would flap: fail, fix, fail, fix...

    A drain that mixes successes and failures must send at most one
    notification, not one per flip.
    """
    channel = _RecordingChannel()
    monkeypatch.setattr(
        "flipp_dl.scheduler.build_notify_channels", lambda settings: [channel]
    )
    _enable_ntfy(repo)

    class _EveryOtherFails(_FakeKomgaClient):
        calls = 0

        def scan_library(self, library_id):
            super().scan_library(library_id)
            _EveryOtherFails.calls += 1
            if _EveryOtherFails.calls % 2:
                raise KomgaError("Komga hiccup")

    _EveryOtherFails.calls = 0
    _FakeKomgaClient.instances.clear()
    monkeypatch.setattr("flipp_dl.scheduler.KomgaClient", _EveryOtherFails)

    repo.set_setting("komga_enabled", "true")
    repo.set_setting("komga_url", "http://localhost:25600")
    repo.set_setting("komga_library_id", "lib-1")
    for i in range(4):
        repo.create_job("komga_sync", {"library_id": f"lib-{i}"})
    repo.session.commit()

    run_komga_sync_queue(session_factory)

    assert len(channel.sent) <= 1, channel.sent


def test_the_web_entrypoint_uses_the_notifying_editions_runner():
    """web/main.py is what runs in production - it must not import the
    unwrapped runner from editions, or a failed run stays silent there."""
    import flipp_dl.scheduler as scheduler
    import flipp_dl.web.main as web_main

    assert web_main.run_editions_queue is scheduler.run_editions_queue
