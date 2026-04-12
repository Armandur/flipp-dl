"""Tests for DownloadRepository using an in-memory SQLite database."""

import pytest
from sqlalchemy.orm import Session

from flipp_dl.db.models import IssueStatus, JobStatus
from flipp_dl.db.repository import DownloadRepository
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


def test_list_publications_watched_only(repo):
    repo.upsert_publication(_publication("A"))
    repo.upsert_publication(_publication("B"))
    repo.session.commit()
    repo.set_watched("A", True)
    repo.session.commit()

    watched = repo.list_publications(watched_only=True)
    assert [p.custom_code for p in watched] == ["A"]


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
