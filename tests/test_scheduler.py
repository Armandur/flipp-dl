"""Tests for the download-queue claiming and startup-recovery logic.

TASK-1282: queued download jobs used to get stuck forever once more than
200 newer jobs (of any type) existed, because ``_claim_next_download_job``
filtered a fixed-size ``list_jobs(limit=200)`` window in Python instead of
querying the DB directly for the oldest queued job.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from flipp_dl.db.models import DbJob, IssueStatus, JobStatus
from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import get_session, make_session_factory
from flipp_dl.models import Issue, Publication
from flipp_dl.scheduler import _claim_next_download_job, recover_stuck_jobs


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
