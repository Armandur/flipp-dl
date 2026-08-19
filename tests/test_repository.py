"""Tests for DownloadRepository using an in-memory SQLite database."""

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from flipp_dl import storage
from flipp_dl.db.models import DbIssue, IssueStatus, JobStatus
from flipp_dl.db.repository import (
    DownloadRepository,
    default_cover_cache_root,
    fetch_and_cache_cover,
)
from flipp_dl.db.session import make_session_factory
from flipp_dl.models import Category, Issue, Publication


@pytest.fixture()
def session() -> Session:
    factory = make_session_factory(":memory:")
    sess = factory()
    yield sess
    sess.close()


@pytest.fixture()
def repo(session: Session) -> DownloadRepository:
    return DownloadRepository(session)


def _publication(code: str = "KA", name: str = "Kalle Anka & Co") -> Publication:
    return Publication(
        custom_code=code,
        name=name,
        categories=[
            Category(id=52, name="Serietidningar"),
            Category(id=7, name="Barn"),
        ],
        issues=[
            Issue(custom_code=f"{code}-01", issue_name="Nr 1", issue_date="2024-01-01"),
            Issue(custom_code=f"{code}-02", issue_name="Nr 2", issue_date="2024-01-15"),
        ],
    )


# ---------------------------------------------------------------------------
# Publications
# ---------------------------------------------------------------------------


def test_upsert_publication_creates_new(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()

    assert db_pub.id is not None
    assert db_pub.custom_code == "KA"
    assert db_pub.name == "Kalle Anka & Co"
    assert len(db_pub.categories) == 2


def test_upsert_publication_is_idempotent(repo):
    pub = _publication()
    repo.upsert_publication(pub)
    repo.session.commit()
    db_pub2 = repo.upsert_publication(pub)
    repo.session.commit()

    assert db_pub2.id is not None
    assert repo.session.query(type(db_pub2)).count() == 1


def test_upsert_publication_updates_name(repo):
    repo.upsert_publication(_publication("KA", "Old Name"))
    repo.session.commit()
    repo.upsert_publication(_publication("KA", "New Name"))
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    assert db_pub.name == "New Name"


def test_set_watched(repo):
    repo.upsert_publication(_publication())
    repo.session.commit()

    assert repo.set_watched("KA", True)
    repo.session.commit()
    assert repo.get_publication("KA").watched is True

    assert repo.set_watched("KA", False)
    repo.session.commit()
    assert repo.get_publication("KA").watched is False


def test_set_watched_unknown_returns_false(repo):
    assert repo.set_watched("NOPE", True) is False


# ---------------------------------------------------------------------------
# Komga series mapping (TASK-1327)
# ---------------------------------------------------------------------------


def test_set_komga_series_id(repo):
    repo.upsert_publication(_publication())
    repo.session.commit()

    assert repo.set_komga_series_id("KA", 55)
    repo.session.commit()

    assert repo.get_publication("KA").komga_series_id == 55


def test_set_komga_series_id_unknown_returns_false(repo):
    assert repo.set_komga_series_id("NOPE", 55) is False


def test_get_unmapped_publications(repo):
    repo.upsert_publication(_publication("KA", "Kalle Anka & Co"))
    repo.upsert_publication(_publication("BAMSE", "Bamse"))
    repo.session.commit()
    repo.set_komga_series_id("KA", 55)
    repo.session.commit()

    unmapped = repo.get_unmapped_publications()

    assert [p.custom_code for p in unmapped] == ["BAMSE"]


def test_get_unmapped_publications_empty_when_all_mapped(repo):
    repo.upsert_publication(_publication())
    repo.session.commit()
    repo.set_komga_series_id("KA", 55)
    repo.session.commit()

    assert repo.get_unmapped_publications() == []


# ---------------------------------------------------------------------------
# Per-publication poll interval (TASK-1291)
# ---------------------------------------------------------------------------


def test_set_publication_poll_interval_unknown_returns_false(repo):
    assert repo.set_publication_poll_interval("NOPE", 30) is False


def test_set_publication_poll_interval_persists(repo):
    repo.upsert_publication(_publication())
    repo.session.commit()

    assert repo.set_publication_poll_interval("KA", 90)
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    assert db_pub.poll_interval_minutes == 90


def test_set_publication_poll_interval_clear_resets_next_due(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    repo.set_publication_poll_interval("KA", 90)
    repo.mark_publication_poll_done(db_pub.id)
    repo.session.commit()
    assert repo.get_publication("KA").next_poll_due_at is not None

    repo.set_publication_poll_interval("KA", None)
    repo.session.commit()

    refreshed = repo.get_publication("KA")
    assert refreshed.poll_interval_minutes is None
    assert refreshed.next_poll_due_at is None


def test_publications_due_for_poll_no_override_always_due(repo):
    """A publication without an override is due on every tick."""
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()

    assert repo.publications_due_for_poll({db_pub.id}) == {db_pub.id}


def test_publications_due_for_poll_not_due_after_being_marked_done(repo):
    """An override publication just marked done isn't due again immediately."""
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    repo.set_publication_poll_interval("KA", 60)
    repo.mark_publication_poll_done(db_pub.id)
    repo.session.commit()

    assert repo.publications_due_for_poll({db_pub.id}) == set()


def test_publications_due_for_poll_due_once_interval_elapsed(repo):
    """An override publication becomes due again once its interval passes."""
    from datetime import datetime, timedelta

    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    repo.set_publication_poll_interval("KA", 60)
    db_pub.next_poll_due_at = datetime.utcnow() - timedelta(minutes=1)
    repo.session.commit()

    assert repo.publications_due_for_poll({db_pub.id}) == {db_pub.id}


def test_mark_publication_poll_done_noop_without_override(repo):
    """No override means nothing to advance - stays always-due."""
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()

    repo.mark_publication_poll_done(db_pub.id)
    repo.session.commit()

    assert repo.get_publication("KA").next_poll_due_at is None


def test_list_publications_watched_only(repo):
    repo.upsert_publication(_publication("A"))
    repo.upsert_publication(_publication("B"))
    repo.session.commit()
    repo.set_watched("A", True)
    repo.session.commit()

    watched = repo.list_publications(watched_only=True)
    assert [p.custom_code for p in watched] == ["A"]


def test_list_publications_counts_issues_without_loading_the_relationship(repo):
    """The counts must come from an aggregated query, not ``len(pub.issues)``.

    ``_publication`` seeds two issues; mark one done so the ratio is 1/2 -
    a plausible bug is reporting "0/2" or "2/2" if the wrong status is
    counted (TASK-1338).
    """
    repo.sync_publications([_publication("KA")])
    repo.session.commit()
    issue = repo.session.scalar(select(DbIssue))

    pubs = repo.list_publications()
    assert len(pubs) == 1
    pub = pubs[0]
    # The relationship must still be unloaded - the counts came from the
    # aggregated query, not from the ORM lazily fetching ``pub.issues``.
    assert "issues" in sa_inspect(pub).unloaded
    assert pub.num_issues == 2
    assert pub.num_downloaded == 0

    repo.mark_issue_done(issue.id, "/tmp/whatever.pdf")
    repo.session.commit()

    pubs2 = repo.list_publications()
    assert pubs2[0].num_downloaded == 1
    assert pubs2[0].num_issues == 2


def test_list_publications_zero_issues_reports_zero(repo):
    repo.upsert_publication(Publication(custom_code="EMPTY", name="Empty", issues=[]))
    repo.session.commit()

    pubs = repo.list_publications()
    assert pubs[0].num_issues == 0
    assert pubs[0].num_downloaded == 0


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def test_upsert_issue_creates_with_status_new(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()

    issue = Issue(custom_code="KA-03", issue_name="Nr 3", issue_date="2024-02-01")
    db_issue, created = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    assert created is True
    assert db_issue.status == IssueStatus.NEW


def test_upsert_issue_is_idempotent(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")
    _, created1 = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()
    _, created2 = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    assert created1 is True
    assert created2 is False


def test_sync_publications_returns_new_issues(repo):
    pub = _publication()
    new_issues = repo.sync_publications([pub])
    repo.session.commit()

    assert len(new_issues) == 2
    assert {i.custom_code for i in new_issues} == {"KA-01", "KA-02"}


def test_sync_publications_second_run_returns_no_new(repo):
    pub = _publication()
    repo.sync_publications([pub])
    repo.session.commit()
    new_issues = repo.sync_publications([pub])
    repo.session.commit()

    assert new_issues == []


def test_issue_status_transitions(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    issue_id = db_issue.id
    repo.mark_issue_downloading(issue_id)
    repo.session.commit()
    assert repo.get_issue(issue_id).status == IssueStatus.DOWNLOADING

    repo.mark_issue_done(issue_id, "/output/KA/Nr1.pdf")
    repo.session.commit()
    db = repo.get_issue(issue_id)
    assert db.status == IssueStatus.DONE
    assert db.file_path == "/output/KA/Nr1.pdf"
    assert db.downloaded_at is not None


def test_update_issue_progress(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    repo.mark_issue_downloading(db_issue.id)
    repo.update_issue_progress(db_issue.id, 3, 10)
    repo.session.commit()

    db = repo.get_issue(db_issue.id)
    assert db.status == IssueStatus.DOWNLOADING
    assert db.progress_current == 3
    assert db.progress_total == 10

    # Completing the download should clear the counters so a later
    # re-download starts from a clean slate.
    repo.mark_issue_done(db_issue.id, "/output/KA/Nr1.pdf")
    repo.session.commit()
    db = repo.get_issue(db_issue.id)
    assert db.progress_current == 0
    assert db.progress_total == 0


def test_mark_issue_error(repo):
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    issue = Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    repo.mark_issue_error(db_issue.id, "HTTP 503")
    repo.session.commit()
    db = repo.get_issue(db_issue.id)
    assert db.status == IssueStatus.ERROR
    assert db.error_message == "HTTP 503"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_round_trip(repo):
    repo.set_setting("token", "abc123")
    repo.session.commit()
    assert repo.get_setting("token") == "abc123"


def test_settings_default_value(repo):
    assert repo.get_setting("missing", "fallback") == "fallback"


def test_settings_overwrite(repo):
    repo.set_setting("key", "v1")
    repo.session.commit()
    repo.set_setting("key", "v2")
    repo.session.commit()
    assert repo.get_setting("key") == "v2"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_job_lifecycle(repo):
    job = repo.create_job("poll", {"publication": "KA"})
    repo.session.commit()
    assert job.status == JobStatus.QUEUED

    repo.start_job(job.id)
    repo.session.commit()
    assert repo.session.get(type(job), job.id).status == JobStatus.RUNNING

    repo.finish_job(job.id)
    repo.session.commit()
    db = repo.session.get(type(job), job.id)
    assert db.status == JobStatus.DONE
    assert db.finished_at is not None


def test_job_error(repo):
    job = repo.create_job("download", {"issue_id": 1})
    repo.session.commit()
    repo.start_job(job.id)
    repo.finish_job(job.id, error="Timeout")
    repo.session.commit()

    db = repo.session.get(type(job), job.id)
    assert db.status == JobStatus.ERROR
    assert db.error_message == "Timeout"


def test_list_jobs_ordered_newest_first(repo):
    repo.create_job("poll")
    repo.session.commit()
    repo.create_job("download")
    repo.session.commit()

    jobs = repo.list_jobs()
    assert jobs[0].job_type == "download"
    assert jobs[1].job_type == "poll"


def test_purge_old_jobs_respects_keep_min(repo):
    # Seed 10 finished jobs and ask to keep at least 5 – nothing should
    # be deleted even if they're "old" because keep_min wins.
    from datetime import datetime, timedelta

    from flipp_dl.db.models import DbJob, JobStatus

    old = datetime.utcnow() - timedelta(days=365)
    for i in range(10):
        repo.session.add(
            DbJob(
                job_type="poll",
                payload="{}",
                status=JobStatus.DONE,
                created_at=old + timedelta(seconds=i),
            )
        )
    repo.session.commit()

    removed = repo.purge_old_jobs(max_age_days=1, keep_min=5)
    repo.session.commit()
    assert removed == 5
    assert repo.session.query(DbJob).count() == 5


def test_purge_old_jobs_keeps_recent(repo):
    # Recent jobs (within max_age_days) are preserved even when
    # keep_min is tiny.
    from datetime import datetime, timedelta

    from flipp_dl.db.models import DbJob, JobStatus

    now = datetime.utcnow()
    for i in range(3):
        repo.session.add(
            DbJob(
                job_type="poll",
                payload="{}",
                status=JobStatus.DONE,
                created_at=now - timedelta(minutes=i),
            )
        )
    repo.session.commit()

    removed = repo.purge_old_jobs(max_age_days=30, keep_min=1)
    repo.session.commit()
    assert removed == 0
    assert repo.session.query(DbJob).count() == 3


def test_purge_old_jobs_never_deletes_running(repo):
    # Stuck RUNNING jobs must survive even if they predate the cutoff.
    from datetime import datetime, timedelta

    from flipp_dl.db.models import DbJob, JobStatus

    old = datetime.utcnow() - timedelta(days=365)
    repo.session.add(
        DbJob(
            job_type="download",
            payload="{}",
            status=JobStatus.RUNNING,
            created_at=old,
        )
    )
    # Add enough finished jobs that keep_min doesn't save them.
    for i in range(5):
        repo.session.add(
            DbJob(
                job_type="poll",
                payload="{}",
                status=JobStatus.DONE,
                created_at=old + timedelta(seconds=i),
            )
        )
    repo.session.commit()

    removed = repo.purge_old_jobs(max_age_days=1, keep_min=0)
    repo.session.commit()
    assert removed == 5
    remaining = list(repo.session.scalars(select(DbJob)))
    assert len(remaining) == 1
    assert remaining[0].status == JobStatus.RUNNING


def test_count_jobs_by_status_covers_every_status(session):
    """Counting happens in the DB and zero-fills unused statuses."""
    repo = DownloadRepository(session)
    for _ in range(3):
        repo.create_job("download", {"issue_id": 1})
    finished = repo.create_job("poll")
    repo.finish_job(finished.id)
    failed = repo.create_job("poll")
    repo.finish_job(failed.id, error="boom")
    session.commit()

    counts = repo.count_jobs_by_status()
    assert counts == {"queued": 3, "running": 0, "done": 1, "error": 1}


def test_list_jobs_filters_by_status_and_type(session):
    repo = DownloadRepository(session)
    repo.create_job("download", {"issue_id": 1})
    done = repo.create_job("poll")
    repo.finish_job(done.id)
    session.commit()

    queued = repo.list_jobs(limit=50, status="queued")
    assert [j.job_type for j in queued] == ["download"]

    polls = repo.list_jobs(limit=50, job_type="poll")
    assert [j.status for j in polls] == ["done"]


def _pub_with_issues(repo, statuses):
    """Seed one publication whose issues have the given statuses."""
    from flipp_dl.models import Issue, Publication

    pub = Publication(custom_code="KA", name="Kalle Anka")
    db_pub = repo.upsert_publication(pub)
    for i, status in enumerate(statuses):
        issue = Issue(
            custom_code=f"ka{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
        )
        db_issue, _ = repo.upsert_issue(issue, db_pub.id)
        db_issue.status = status
    repo.session.flush()
    return db_pub


def test_queue_missing_issues_queues_not_downloaded_and_failed(session):
    repo = DownloadRepository(session)
    pub = _pub_with_issues(
        repo, ["new", "done", "error", "queued", "downloading", "new"]
    )

    queued = repo.queue_missing_issues(pub.id)
    session.commit()

    # Two new + one error; done/queued/downloading are left alone.
    assert queued == 3
    assert repo.count_jobs_by_status()["queued"] == 3
    statuses = sorted(i.status for i in repo.list_issues(publication_id=pub.id))
    assert statuses == ["done", "downloading", "queued", "queued", "queued", "queued"]


def test_queue_missing_issues_can_skip_failed(session):
    """Polls must not re-queue an issue that keeps failing."""
    repo = DownloadRepository(session)
    pub = _pub_with_issues(repo, ["new", "error"])

    queued = repo.queue_missing_issues(pub.id, include_failed=False)
    session.commit()

    assert queued == 1
    assert repo.count_jobs_by_status()["queued"] == 1


def test_queue_missing_issues_is_idempotent(session):
    repo = DownloadRepository(session)
    pub = _pub_with_issues(repo, ["new", "new"])

    first = repo.queue_missing_issues(pub.id)
    second = repo.queue_missing_issues(pub.id)
    session.commit()

    assert (first, second) == (2, 0)
    assert repo.count_jobs_by_status()["queued"] == 2


# ---------------------------------------------------------------------------
# import_existing_files (TASK-1283)
# ---------------------------------------------------------------------------


def test_import_existing_backfills_a_queued_issue_found_on_disk(repo, tmp_path):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    issue = pub.issues[0]
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    target = storage.issue_path(tmp_path, pub, issue)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert len(report.backfilled) == 1
    assert report.backfilled[0]["issue_id"] == db_issue.id
    refreshed = repo.get_issue(db_issue.id)
    assert refreshed.status == IssueStatus.DONE
    assert refreshed.file_path == str(target.resolve())
    assert refreshed.downloaded_at is not None
    assert report.orphan_files == []
    assert report.missing_files == []
    assert report.shared_files == []


def test_import_existing_leaves_an_already_done_issue_alone(repo, tmp_path):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    issue = pub.issues[0]
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()

    target = storage.issue_path(tmp_path, pub, issue)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")
    repo.mark_issue_done(db_issue.id, str(target.resolve()))
    repo.session.commit()

    report = repo.import_existing_files(tmp_path)

    assert report.backfilled == []
    assert report.missing_files == []


def test_import_existing_reports_orphan_files(repo, tmp_path):
    """A file on disk that matches no issue - never downloaded via us."""
    (tmp_path / "Unknown Publication").mkdir()
    orphan = tmp_path / "Unknown Publication" / "Nr 1.pdf"
    orphan.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)

    assert report.orphan_files == ["Unknown Publication/Nr 1.pdf"]
    assert report.backfilled == []


def test_import_existing_reports_missing_files(repo, tmp_path):
    """A 'done' issue whose file has vanished from disk (driftfynd 2026-08-18)."""
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    issue = pub.issues[0]
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.mark_issue_done(db_issue.id, str(tmp_path / "Kalle Anka och Co" / "gone.pdf"))
    repo.session.commit()

    report = repo.import_existing_files(tmp_path)

    assert len(report.missing_files) == 1
    assert report.missing_files[0]["issue_id"] == db_issue.id


def test_import_existing_reports_issues_sharing_one_file(repo, tmp_path):
    """Two issues pointing at the same file - one was never really downloaded."""
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    a, b = pub.issues
    db_a, _ = repo.upsert_issue(a, db_pub.id)
    db_b, _ = repo.upsert_issue(b, db_pub.id)
    shared = str(tmp_path / "Kalle Anka och Co" / "shared.pdf")
    repo.mark_issue_done(db_a.id, shared)
    repo.mark_issue_done(db_b.id, shared)
    repo.session.commit()

    report = repo.import_existing_files(tmp_path)

    assert len(report.shared_files) == 1
    ids = {i["issue_id"] for i in report.shared_files[0]["issues"]}
    assert ids == {db_a.id, db_b.id}


def test_import_existing_does_not_hand_a_claimed_file_to_another_issue(repo, tmp_path):
    """A file already owned by one issue must not be handed to a second.

    Regression for the exact bug this task exists to catch: two issues
    with the same name+date collide on the plain filename (TASK-1349).
    The higher-id issue is the one that actually downloaded and owns
    the file in the DB; the lower-id issue is still ``queued`` and has
    no file of its own. Because ``by_path`` picks the lower-id issue as
    the "canonical" candidate for that filename, a naive import would
    mark the lower-id issue done on top of the real owner's file -
    recreating the drift instead of reporting it.
    """
    pub = Publication(
        custom_code="SHARE",
        name="Shared Pub",
        issues=[
            Issue(
                custom_code="share-01", issue_name="Nr 6 2024", issue_date="2024-03-01"
            ),
            Issue(
                custom_code="share-02", issue_name="Nr 6 2024", issue_date="2024-03-01"
            ),
        ],
    )
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    low, _ = repo.upsert_issue(pub.issues[0], db_pub.id)  # lower id, still queued
    high, _ = repo.upsert_issue(pub.issues[1], db_pub.id)  # higher id, real owner
    repo.mark_issue_queued(low.id)

    target = storage.issue_path(tmp_path, pub, pub.issues[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")
    repo.mark_issue_done(high.id, str(target.resolve()))
    repo.session.commit()

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert report.backfilled == []
    assert repo.get_issue(low.id).status == IssueStatus.QUEUED
    assert repo.get_issue(low.id).file_path is None
    assert len(report.shared_files) == 1
    ids = {i["issue_id"] for i in report.shared_files[0]["issues"]}
    assert ids == {low.id, high.id}


def test_import_existing_ignores_a_symlink_that_escapes_output_root(repo, tmp_path):
    """A path resolving outside output_root must never mark an issue done."""
    output_root = tmp_path / "output"
    output_root.mkdir()
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    issue = pub.issues[0]
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4\n%dummy\n")
    link = output_root / storage.publication_folder(output_root, pub).name
    link.mkdir()
    escaping = link / storage.issue_filename(pub, issue)
    try:
        escaping.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    report = repo.import_existing_files(output_root)

    assert report.backfilled == []
    assert report.orphan_files == []
    assert repo.get_issue(db_issue.id).status == IssueStatus.QUEUED


# ---------------------------------------------------------------------------
# Cover cache (TASK-1345)
# ---------------------------------------------------------------------------


class _FakeCoverResponse:
    def __init__(self, content: bytes, content_type: str, status: int = 200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


class _FakeCoverSession:
    """Stand-in for requests.Session - no network access in tests."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.requested_urls: list[str] = []

    def get(self, url, timeout=None):
        self.requested_urls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._response


def test_default_cover_cache_root_honours_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("FLIPP_COVER_CACHE", str(tmp_path / "covers"))
    assert default_cover_cache_root() == tmp_path / "covers"


def test_default_cover_cache_root_falls_back_next_to_the_db(monkeypatch, tmp_path):
    monkeypatch.delenv("FLIPP_COVER_CACHE", raising=False)
    monkeypatch.setenv("FLIPP_DB", str(tmp_path / "flipp.db"))
    assert default_cover_cache_root() == tmp_path / "flipp-dl-covers"


def test_fetch_and_cache_cover_saves_the_file(tmp_path):
    session = _FakeCoverSession(_FakeCoverResponse(b"\xff\xd8\xff", "image/jpeg"))
    filename = fetch_and_cache_cover(
        "https://example.invalid/cover.jpg",
        tmp_path,
        "pub-KA",
        http_session=session,
    )
    assert filename == "pub-KA.jpg"
    assert (tmp_path / filename).read_bytes() == b"\xff\xd8\xff"
    # No leftover temp file.
    assert list(tmp_path.glob("*.tmp")) == []


def test_fetch_and_cache_cover_rejects_non_image_content_type(tmp_path):
    session = _FakeCoverSession(_FakeCoverResponse(b"<html>", "text/html"))
    filename = fetch_and_cache_cover(
        "https://example.invalid/cover.jpg",
        tmp_path,
        "pub-KA",
        http_session=session,
    )
    assert filename is None
    assert list(tmp_path.glob("*")) == []


def test_fetch_and_cache_cover_returns_none_on_network_error(tmp_path):
    import requests

    session = _FakeCoverSession(exc=requests.ConnectionError("boom"))
    filename = fetch_and_cache_cover(
        "https://example.invalid/cover.jpg",
        tmp_path,
        "pub-KA",
        http_session=session,
    )
    assert filename is None


def test_fetch_and_cache_cover_returns_none_on_http_error(tmp_path):
    session = _FakeCoverSession(_FakeCoverResponse(b"", "image/jpeg", status=404))
    filename = fetch_and_cache_cover(
        "https://example.invalid/cover.jpg",
        tmp_path,
        "pub-KA",
        http_session=session,
    )
    assert filename is None


def test_publications_needing_cover_refresh_finds_uncached(repo):
    pub = _publication()
    pub.cover_url = "https://example.invalid/a.jpg"
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()

    stale = repo.publications_needing_cover_refresh()
    assert [p.id for p in stale] == [db_pub.id]


def test_publications_needing_cover_refresh_skips_already_cached(repo):
    pub = _publication()
    pub.cover_url = "https://example.invalid/a.jpg"
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    repo.set_publication_cover_cache(db_pub.id, "pub-KA.jpg", pub.cover_url)
    repo.session.commit()

    assert repo.publications_needing_cover_refresh() == []


def test_publications_needing_cover_refresh_picks_up_a_changed_url(repo):
    pub = _publication()
    pub.cover_url = "https://example.invalid/a.jpg"
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    repo.set_publication_cover_cache(db_pub.id, "pub-KA.jpg", pub.cover_url)
    repo.session.commit()

    pub.cover_url = "https://example.invalid/b.jpg"
    repo.upsert_publication(pub)
    repo.session.commit()

    stale = repo.publications_needing_cover_refresh()
    assert [p.id for p in stale] == [db_pub.id]


def test_set_issue_cover_cache_stores_the_filename(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    repo.session.commit()

    repo.set_issue_cover_cache(db_issue.id, "issue-KA-01.jpg")
    repo.session.commit()

    assert repo.get_issue(db_issue.id).cover_cache_path == "issue-KA-01.jpg"


def test_zero_poll_interval_counts_as_no_override(session):
    """A stored 0 must not make a publication permanently due-but-unmarked.

    The route normalises 0 away, but the two call sites used to disagree
    about what 0 means - due_for_poll treated it as an override while
    mark_poll_done ignored it.
    """
    from flipp_dl.models import Publication

    repo = DownloadRepository(session)
    db_pub = repo.upsert_publication(Publication(custom_code="KA", name="Kalle Anka"))
    db_pub.poll_interval_minutes = 0
    session.flush()

    assert repo.publications_due_for_poll({db_pub.id}) == {db_pub.id}
    repo.mark_publication_poll_done(db_pub.id)
    # Still due: 0 means "no override" on both sides.
    assert repo.publications_due_for_poll({db_pub.id}) == {db_pub.id}
    assert db_pub.next_poll_due_at is None
