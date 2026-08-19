"""Tests for the download-queue claiming and startup-recovery logic.

TASK-1282: queued download jobs used to get stuck forever once more than
200 newer jobs (of any type) existed, because ``_claim_next_download_job``
filtered a fixed-size ``list_jobs(limit=200)`` window in Python instead of
querying the DB directly for the oldest queued job.
"""

import io
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from flipp_dl.db.models import DbJob, IssueStatus, JobStatus
from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session, make_session_factory
from flipp_dl.komga import KomgaError
from flipp_dl.models import Issue, Publication
from flipp_dl.scheduler import (
    _claim_next_download_job,
    recover_stuck_jobs,
    run_download_queue,
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
    job_id, _domain_pub, _domain_issue = claimed
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


def test_poll_backfills_watched_publications(repo, session_factory, monkeypatch):
    """A watched publication catches up on issues it never downloaded."""
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
    """Records scan_library calls; monkeypatched in for KomgaClient."""

    instances: list["_FakeKomgaClient"] = []

    def __init__(self, url, *, username="", password="", api_key=""):
        self.url = url
        self.username = username
        self.password = password
        self.api_key = api_key
        self.scanned: list[str] = []
        _FakeKomgaClient.instances.append(self)

    def scan_library(self, library_id):
        self.scanned.append(library_id)


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
        assert komga_job.payload == '{"library_id": "lib-1"}'


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
