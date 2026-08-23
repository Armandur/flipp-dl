"""Tests for DownloadRepository using an in-memory SQLite database."""

import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from flipp_dl import storage
from flipp_dl.db.models import DbIssue, DbJob, IssueStatus, JobStatus
from flipp_dl.db.repository import (
    MAX_AUTO_RETRIES,
    RETRY_DELAYS_MINUTES,
    DownloadRepository,
    PublicationDestinationError,
    PublicationFolderConflict,
    PublicationFolderError,
    PublicationFolderMoveError,
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


def _downloaded_publication(
    repo: DownloadRepository, root: Path, *, issue_count: int = 2
) -> tuple[list[int], list[Path]]:
    publication = _publication("A", "Hjemmet")
    db_publication = repo.upsert_publication(publication)
    issue_ids = []
    paths = []
    for issue in publication.issues[:issue_count]:
        db_issue, _ = repo.upsert_issue(issue, db_publication.id)
        path = storage.issue_path(root, db_publication, db_issue)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"issue-{db_issue.id}".encode())
        repo.mark_issue_done(db_issue.id, str(path.resolve()))
        issue_ids.append(db_issue.id)
        paths.append(path.resolve())
    repo.session.commit()
    return issue_ids, paths


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


def test_publication_folder_name_is_validated_and_must_be_unique(repo, tmp_path):
    repo.upsert_publication(_publication("A", "Hjemmet"))
    repo.upsert_publication(_publication("B", "Hjemmet"))
    repo.session.commit()

    with pytest.raises(PublicationFolderError):
        repo.set_publication_folder_name("B", "../Hjemmet", tmp_path)
    with pytest.raises(PublicationFolderConflict):
        repo.set_publication_folder_name("B", "Hjemmet", tmp_path)

    assert repo.set_publication_folder_name("B", "Hjemmet (DK)", tmp_path)
    repo.session.commit()
    assert repo.get_publication("B").folder_name == "Hjemmet (DK)"


def test_setting_folder_name_moves_downloaded_files_and_updates_path(repo, tmp_path):
    pub = _publication("A", "Hjemmet")
    db_pub = repo.upsert_publication(pub)
    db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    old_path = storage.issue_path(tmp_path, db_pub, db_issue)
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"%PDF-1.4\n")
    repo.mark_issue_done(db_issue.id, str(old_path.resolve()))
    repo.session.commit()

    report = repo.set_publication_folder_name("A", "Hjemmet (NO)", tmp_path)
    repo.session.commit()

    assert report.moved == 1
    assert report.failed == []
    moved = tmp_path / "Hjemmet (NO)" / old_path.name
    assert moved.is_file()
    assert not old_path.exists()
    assert repo.get_issue(db_issue.id).file_path == str(moved.resolve())

def test_destination_change_moves_files_between_roots(repo, tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    issue_ids, sources = _downloaded_publication(repo, primary)

    report = repo.set_publication_destination(
        "A", "secondary", primary, secondary
    )

    assert report.moved == 2
    assert report.failed == []
    for issue_id, source in zip(issue_ids, sources, strict=True):
        target = secondary / "Hjemmet" / source.name
        assert target.is_file()
        assert not source.exists()
        assert repo.get_issue(issue_id).file_path == str(target.resolve())


def test_destination_change_copies_verifies_and_deletes_on_exdev(
    repo, tmp_path, monkeypatch
):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    issue_ids, sources = _downloaded_publication(repo, primary, issue_count=1)
    source = sources[0]
    original_replace = Path.replace

    def raise_exdev(path, target):
        if path == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", raise_exdev)

    report = repo.set_publication_destination(
        "A", "secondary", primary, secondary
    )

    target = secondary / "Hjemmet" / source.name
    assert report.moved == 1
    assert report.failed == []
    assert target.read_bytes() == f"issue-{issue_ids[0]}".encode()
    assert not source.exists()
    assert repo.get_issue(issue_ids[0]).file_path == str(target.resolve())


def test_interrupted_move_keeps_each_issue_consistent_and_can_resume(
    repo, tmp_path, monkeypatch
):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    issue_ids, sources = _downloaded_publication(repo, primary)
    original_move = storage.move_file
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        original_move(source, target)

    monkeypatch.setattr(storage, "move_file", fail_second)
    first_report = repo.set_publication_destination(
        "A", "secondary", primary, secondary
    )

    first_target = secondary / "Hjemmet" / sources[0].name
    assert first_report.moved == 1
    assert first_report.failed == [str(sources[1])]
    assert first_target.is_file()
    assert repo.get_issue(issue_ids[0]).file_path == str(first_target.resolve())
    assert sources[1].is_file()
    assert repo.get_issue(issue_ids[1]).file_path == str(sources[1])

    monkeypatch.setattr(storage, "move_file", original_move)
    second_report = repo.set_publication_destination(
        "A", "secondary", primary, secondary
    )

    second_target = secondary / "Hjemmet" / sources[1].name
    assert second_report.moved == 1
    assert second_report.failed == []
    assert second_target.is_file()
    assert not sources[1].exists()
    assert repo.get_issue(issue_ids[1]).file_path == str(second_target.resolve())


def test_restart_accepts_file_already_at_target(repo, tmp_path):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    issue_ids, sources = _downloaded_publication(repo, primary, issue_count=1)
    source = sources[0]
    target = secondary / "Hjemmet" / source.name
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())

    report = repo.set_publication_destination(
        "A", "secondary", primary, secondary
    )

    assert report.moved == 0
    assert report.failed == []
    assert target.is_file()
    assert not source.exists()
    assert repo.get_issue(issue_ids[0]).file_path == str(target.resolve())


@pytest.mark.parametrize(
    "job_status",
    [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRY_PENDING],
)
@pytest.mark.parametrize(
    ("operation", "error_type"),
    [
        ("folder", PublicationFolderMoveError),
        ("destination", PublicationDestinationError),
    ],
)
def test_publication_move_is_blocked_by_active_download_job(
    repo, tmp_path, job_status, operation, error_type
):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    issue_ids, sources = _downloaded_publication(repo, primary, issue_count=1)
    job = repo.create_job("download", {"issue_id": issue_ids[0]})
    job.status = job_status
    repo.session.commit()

    # Asserted on the reason, not the prose: the message is translated in
    # the web layer and must be free to change without breaking this.
    with pytest.raises(error_type) as excinfo:
        if operation == "folder":
            repo.set_publication_folder_name(
                "A", "Nya Hjemmet", primary, secondary
            )
        else:
            repo.set_publication_destination(
                "A", "secondary", primary, secondary
            )

    assert excinfo.value.reason == "download_active"
    assert sources[0].is_file()
    publication = repo.get_publication("A")
    assert publication.folder_name is None
    assert publication.destination is None



def test_folder_name_reaches_detached_domain_publication(repo, tmp_path):
    db_pub = repo.upsert_publication(_publication("A", "Hjemmet"))
    repo.set_publication_folder_name("A", "Hjemmet (NO)", tmp_path)
    repo.session.commit()

    detached = Publication(custom_code=db_pub.custom_code, name=db_pub.name)

    assert storage.publication_folder(tmp_path, detached) == tmp_path / "Hjemmet (NO)"


def test_sync_pauses_watched_publications_when_names_later_collide(repo):
    first = repo.upsert_publication(_publication("A", "Hjemmet Norge"))
    second = repo.upsert_publication(_publication("B", "Hjemmet Danmark"))
    first.watched = True
    second.watched = True
    repo.session.commit()

    repo.sync_publications([_publication("A", "Hjemmet"), _publication("B", "Hjemmet")])
    repo.session.commit()

    assert repo.get_publication("A").watched is False
    assert repo.get_publication("B").watched is False


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
# Komga read status (TASK-1328)
# ---------------------------------------------------------------------------


def test_set_komga_book_id(repo):
    repo.sync_publications([_publication()])
    repo.session.commit()
    issue_id = repo.get_publication("KA").issues[0].id

    assert repo.set_komga_book_id(issue_id, 99)
    repo.session.commit()

    assert repo.get_issue(issue_id).komga_book_id == 99


def test_set_komga_book_id_unknown_returns_false(repo):
    assert repo.set_komga_book_id(999999, 99) is False


def test_get_issues_with_komga_book_id_only_returns_mapped(repo):
    repo.sync_publications([_publication()])
    repo.session.commit()
    mapped_id = repo.get_publication("KA").issues[0].id
    repo.set_komga_book_id(mapped_id, 99)
    repo.session.commit()

    mapped = repo.get_issues_with_komga_book_id()

    assert [i.id for i in mapped] == [mapped_id]


def test_set_issue_read_status(repo):
    from datetime import datetime

    repo.sync_publications([_publication()])
    repo.session.commit()
    issue_id = repo.get_publication("KA").issues[0].id
    synced_at = datetime(2026, 8, 19, 12, 0, 0)

    assert repo.set_issue_read_status(issue_id, read=True, page=24, synced_at=synced_at)
    repo.session.commit()

    db_issue = repo.get_issue(issue_id)
    assert db_issue.komga_read is True
    assert db_issue.komga_read_page == 24
    assert db_issue.komga_read_synced_at == synced_at


def test_set_issue_read_status_unknown_returns_false(repo):
    from datetime import datetime

    assert (
        repo.set_issue_read_status(
            999999, read=True, page=1, synced_at=datetime(2026, 8, 19)
        )
        is False
    )


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


def test_list_publications_sums_size_bytes_from_the_aggregated_query(repo):
    """size_bytes/size_unknown_count come from the same GROUP BY query as
    num_issues/num_downloaded (TASK-1379) - not a per-publication round trip.
    """
    repo.sync_publications([_publication("KA")])
    repo.session.commit()
    issues = list(repo.session.scalars(select(DbIssue)))
    repo.mark_issue_done(issues[0].id, "/tmp/a.pdf", file_size=1000)
    # Second issue done but predates TASK-1362 - no known size.
    repo.mark_issue_done(issues[1].id, "/tmp/b.pdf", file_size=None)
    repo.session.commit()

    pubs = repo.list_publications()
    assert len(pubs) == 1
    pub = pubs[0]
    assert "issues" in sa_inspect(pub).unloaded
    assert pub.size_bytes == 1000
    assert pub.size_unknown_count == 1


def test_list_publications_size_unknown_count_zero_when_all_sizes_known(repo):
    pub = _pub_with_sized_issues(
        repo, [(IssueStatus.DONE, 100), (IssueStatus.DONE, 200)]
    )
    repo.session.commit()

    pubs = repo.list_publications()
    assert pubs[0].id == pub.id
    assert pubs[0].size_bytes == 300
    assert pubs[0].size_unknown_count == 0


def test_list_publications_size_bytes_zero_when_no_downloaded_issues(repo):
    repo.upsert_publication(Publication(custom_code="EMPTY", name="Empty", issues=[]))
    repo.session.commit()

    pubs = repo.list_publications()
    assert pubs[0].size_bytes == 0
    assert pubs[0].size_unknown_count == 0


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


def test_sync_publications_marks_missing_publication_delisted(repo):
    """A publication absent from a non-empty response is delisted (TASK-1426)."""
    ka = _publication("KA", "Kalle Anka & Co")
    frost = _publication("FROST", "Frost Aktivitetspåse")
    repo.sync_publications([ka, frost])
    repo.session.commit()

    # Frost drops out of this poll's response - only KA remains.
    repo.sync_publications([ka])
    repo.session.commit()

    db_frost = repo.get_publication("FROST")
    db_ka = repo.get_publication("KA")
    assert db_frost.delisted_at is not None
    assert db_ka.delisted_at is None


def test_sync_publications_clears_delisted_when_seen_again(repo):
    ka = _publication("KA")
    frost = _publication("FROST", "Frost Aktivitetspåse")
    repo.sync_publications([ka, frost])
    repo.session.commit()
    repo.sync_publications([ka])
    repo.session.commit()
    assert repo.get_publication("FROST").delisted_at is not None

    # Frost reappears in a later poll - the mark clears automatically.
    repo.sync_publications([ka, frost])
    repo.session.commit()

    assert repo.get_publication("FROST").delisted_at is None


def test_sync_publications_empty_response_does_not_delist_everything(repo):
    """An empty (but non-erroring) API response must never nuke the list.

    poll_publications already aborts before sync_publications on a hard
    FlippError - this guards the remaining risk of a technically
    successful but empty/degenerate response.
    """
    ka = _publication("KA")
    frost = _publication("FROST", "Frost Aktivitetspåse")
    repo.sync_publications([ka, frost])
    repo.session.commit()

    repo.sync_publications([])
    repo.session.commit()

    assert repo.get_publication("KA").delisted_at is None
    assert repo.get_publication("FROST").delisted_at is None


def test_sync_publications_does_not_delete_delisted_publication(repo):
    """Nothing about delisting removes the row, its issues, or downloads."""
    frost = _publication("FROST", "Frost Aktivitetspåse")
    repo.sync_publications([frost])
    repo.session.commit()
    db_frost = repo.get_publication("FROST")
    issue = db_frost.issues[0]
    issue.status = IssueStatus.DONE
    issue.file_path = "/downloads/frost/nr1.pdf"
    repo.session.commit()

    repo.sync_publications([_publication("KA")])
    repo.session.commit()

    db_frost = repo.get_publication("FROST")
    assert db_frost is not None
    assert db_frost.delisted_at is not None
    assert len(db_frost.issues) == 2
    done_issue = next(i for i in db_frost.issues if i.custom_code == issue.custom_code)
    assert done_issue.status == IssueStatus.DONE
    assert done_issue.file_path == "/downloads/frost/nr1.pdf"


def test_sync_publications_marks_missing_issue_delisted(repo):
    """An issue absent from a non-empty issue list is delisted (TASK-1429)."""
    pub = _publication()
    repo.sync_publications([pub])
    repo.session.commit()

    # KA-02 drops out of this poll's response - only KA-01 remains.
    only_first = Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        issues=[Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")],
    )
    repo.sync_publications([only_first])
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    issue1 = next(i for i in db_pub.issues if i.custom_code == "KA-01")
    issue2 = next(i for i in db_pub.issues if i.custom_code == "KA-02")
    assert issue1.delisted_at is None
    assert issue2.delisted_at is not None


def test_sync_publications_clears_issue_delisted_when_seen_again(repo):
    pub = _publication()
    repo.sync_publications([pub])
    repo.session.commit()

    only_first = Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        issues=[Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")],
    )
    repo.sync_publications([only_first])
    repo.session.commit()
    db_pub = repo.get_publication("KA")
    issue2 = next(i for i in db_pub.issues if i.custom_code == "KA-02")
    assert issue2.delisted_at is not None

    # KA-02 reappears in a later poll - the mark clears automatically.
    repo.sync_publications([pub])
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    issue2 = next(i for i in db_pub.issues if i.custom_code == "KA-02")
    assert issue2.delisted_at is None


def test_sync_publications_empty_issue_list_does_not_delist_all_issues(repo):
    """A publication that syncs with an empty issue list must not read as
    "every one of its issues vanished" - only a hard failure or an
    empty *publications* response are already excluded upstream; this
    guards the remaining "one publication's issue list came back empty"
    case.
    """
    pub = _publication()
    repo.sync_publications([pub])
    repo.session.commit()

    empty_issues = Publication(custom_code="KA", name="Kalle Anka & Co", issues=[])
    repo.sync_publications([empty_issues])
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    assert all(i.delisted_at is None for i in db_pub.issues)


def test_sync_publications_does_not_delete_delisted_issue(repo):
    """Nothing about delisting an issue removes the row or its download."""
    pub = _publication()
    repo.sync_publications([pub])
    repo.session.commit()
    db_pub = repo.get_publication("KA")
    issue2 = next(i for i in db_pub.issues if i.custom_code == "KA-02")
    issue2.status = IssueStatus.DONE
    issue2.file_path = "/downloads/ka/nr2.pdf"
    repo.session.commit()

    only_first = Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        issues=[Issue(custom_code="KA-01", issue_name="Nr 1", issue_date="2024-01-01")],
    )
    repo.sync_publications([only_first])
    repo.session.commit()

    db_pub = repo.get_publication("KA")
    assert len(db_pub.issues) == 2
    issue2 = next(i for i in db_pub.issues if i.custom_code == "KA-02")
    assert issue2.delisted_at is not None
    assert issue2.status == IssueStatus.DONE
    assert issue2.file_path == "/downloads/ka/nr2.pdf"


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


# ---------------------------------------------------------------------------
# Automatic retry (TASK-1363)
# ---------------------------------------------------------------------------


def _seed_issue_for_retry(repo, custom_code: str = "KA-01") -> int:
    db_pub = repo.upsert_publication(_publication())
    repo.session.commit()
    issue = Issue(custom_code=custom_code, issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)
    repo.session.commit()
    return db_issue.id


def test_schedule_issue_retry_grows_delay_each_attempt(repo):
    issue_id = _seed_issue_for_retry(repo)
    repo.mark_issue_error(issue_id, "database is locked")
    repo.session.commit()

    seen_delays = []
    for expected_attempt, expected_delay in enumerate(RETRY_DELAYS_MINUTES, start=1):
        before = datetime.now(UTC).replace(tzinfo=None)
        scheduled = repo.schedule_issue_retry(issue_id, "database is locked")
        repo.session.commit()
        assert scheduled is True

        issue = repo.get_issue(issue_id)
        assert issue.status == IssueStatus.RETRY_PENDING
        assert issue.retry_count == expected_attempt
        assert issue.next_retry_at is not None
        actual_delay = (issue.next_retry_at - before).total_seconds() / 60
        # Loose bound - just confirms the right bucket, not exact timing.
        assert abs(actual_delay - expected_delay) < 1
        seen_delays.append(expected_delay)

    # Delays strictly grow - the whole point of backoff.
    assert seen_delays == sorted(seen_delays)

    # One more failure past MAX_AUTO_RETRIES: no further retry, issue
    # would be left as whatever mark_issue_error set it to (ERROR).
    repo.mark_issue_error(issue_id, "database is locked")
    repo.session.commit()
    scheduled = repo.schedule_issue_retry(issue_id, "database is locked")
    repo.session.commit()
    assert scheduled is False
    assert repo.get_issue(issue_id).status == IssueStatus.ERROR
    assert repo.get_issue(issue_id).retry_count == MAX_AUTO_RETRIES


def test_mark_issue_done_resets_retry_bookkeeping(repo):
    issue_id = _seed_issue_for_retry(repo)
    repo.mark_issue_error(issue_id, "boom")
    repo.schedule_issue_retry(issue_id, "boom")
    repo.session.commit()
    assert repo.get_issue(issue_id).retry_count == 1

    repo.mark_issue_done(issue_id, "/output/KA/Nr1.pdf")
    repo.session.commit()
    issue = repo.get_issue(issue_id)
    assert issue.retry_count == 0
    assert issue.next_retry_at is None


def test_mark_issue_queued_resets_retry_bookkeeping(repo):
    """A deliberate re-queue (manual click, backfill) is a fresh start.

    Only the automatic backoff requeue (``requeue_due_retries``) must
    keep counting against the same budget - see that method's docstring.
    """
    issue_id = _seed_issue_for_retry(repo)
    repo.mark_issue_error(issue_id, "boom")
    repo.schedule_issue_retry(issue_id, "boom")
    repo.session.commit()
    assert repo.get_issue(issue_id).retry_count == 1

    repo.mark_issue_queued(issue_id)
    repo.session.commit()
    issue = repo.get_issue(issue_id)
    assert issue.status == IssueStatus.QUEUED
    assert issue.retry_count == 0
    assert issue.next_retry_at is None


def test_requeue_due_retries_only_picks_up_elapsed_ones(repo):
    due_id = _seed_issue_for_retry(repo)
    repo.mark_issue_error(due_id, "boom")
    repo.schedule_issue_retry(due_id, "boom")
    repo.session.commit()
    # Force it into the past so it's due right now.
    issue = repo.get_issue(due_id)
    issue.next_retry_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    repo.session.commit()

    not_due_id = _seed_issue_for_retry(repo, custom_code="KA-02")
    repo.mark_issue_error(not_due_id, "boom")
    repo.schedule_issue_retry(not_due_id, "boom")
    repo.session.commit()  # next_retry_at is minutes in the future - not due

    requeued = repo.requeue_due_retries()
    repo.session.commit()
    assert requeued == 1

    due = repo.get_issue(due_id)
    assert due.status == IssueStatus.QUEUED
    assert due.next_retry_at is None
    assert due.retry_count == 1  # preserved, not reset

    not_due = repo.get_issue(not_due_id)
    assert not_due.status == IssueStatus.RETRY_PENDING

    jobs = list(repo.session.execute(select(DbIssue)))  # sanity: still one issue row
    assert len(jobs) == 2


def test_reset_orphaned_issues_leaves_retry_pending_alone(repo):
    """A RETRY_PENDING issue has no job behind it by design - it must
    survive the startup sweep that resets orphaned QUEUED/DOWNLOADING
    rows, or an issue waiting for its backoff window would be silently
    bounced back to NEW every restart (TASK-1363)."""
    issue_id = _seed_issue_for_retry(repo)
    repo.mark_issue_error(issue_id, "boom")
    repo.schedule_issue_retry(issue_id, "boom")
    repo.session.commit()
    assert repo.get_issue(issue_id).status == IssueStatus.RETRY_PENDING

    reset = repo.reset_orphaned_issues()
    repo.session.commit()

    assert reset == 0
    assert repo.get_issue(issue_id).status == IssueStatus.RETRY_PENDING


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


def _downloaded_issues_for_komga(repo, count=2):
    pub = repo.upsert_publication(Publication(custom_code="KG", name="Komga"))
    issues = []
    for index in range(count):
        issue = Issue(
            custom_code=f"kg-{index}",
            issue_name=f"Nr {index}",
            issue_date="2024-01-01",
        )
        db_issue, _ = repo.upsert_issue(issue, pub.id)
        db_issue.status = IssueStatus.DONE
        db_issue.file_path = f"/downloads/kg-{index}.pdf"
        issues.append(db_issue)
    repo.session.flush()
    return issues


def test_queue_komga_backfill_queues_only_unmapped_downloaded_issues(repo):
    unmapped, mapped = _downloaded_issues_for_komga(repo)
    mapped.komga_book_id = 42
    repo.session.commit()

    queued, remaining = repo.queue_komga_backfill("library-1", 500)
    repo.session.commit()

    jobs = list(repo.session.scalars(select(DbJob)))
    assert (queued, remaining) == (1, 0)
    assert len(jobs) == 1
    assert jobs[0].job_type == "komga_sync"
    assert jobs[0].status == JobStatus.QUEUED
    assert jobs[0].payload == json.dumps(
        {"library_id": "library-1", "issue_id": unmapped.id}
    )


def test_queue_komga_backfill_skips_an_existing_active_job(repo):
    (issue,) = _downloaded_issues_for_komga(repo, count=1)
    repo.session.commit()

    assert repo.queue_komga_backfill("library-1", 500) == (1, 0)
    assert repo.queue_komga_backfill("library-1", 500) == (0, 0)
    repo.session.commit()

    assert repo.session.query(DbJob).count() == 1
    assert (
        json.loads(repo.session.scalar(select(DbJob.payload)))["issue_id"] == issue.id
    )


def test_queue_komga_backfill_retries_an_issue_with_only_an_error_job(repo):
    (issue,) = _downloaded_issues_for_komga(repo, count=1)
    old_job = repo.create_job(
        "komga_sync", {"library_id": "library-1", "issue_id": issue.id}
    )
    repo.finish_job(old_job.id, error="not found")
    repo.session.commit()

    assert repo.queue_komga_backfill("library-1", 500) == (1, 0)
    repo.session.commit()

    jobs = list(repo.session.scalars(select(DbJob).order_by(DbJob.id)))
    assert [job.status for job in jobs] == [JobStatus.ERROR, JobStatus.QUEUED]


def test_queue_komga_backfill_respects_limit_and_reports_remaining(repo):
    _downloaded_issues_for_komga(repo, count=4)
    repo.session.commit()

    assert repo.queue_komga_backfill("library-1", 2) == (2, 2)
    repo.session.commit()

    assert repo.session.query(DbJob).count() == 2


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

    parked = repo.create_job("komga_sync", {"issue_id": 1})
    assert repo.schedule_job_retry(parked.id, "not found in Komga")
    session.commit()

    counts = repo.count_jobs_by_status()
    # Asking the enum rather than listing the names by hand: a status
    # added later must show up here instead of silently going uncounted.
    assert set(counts) == {status.value for status in JobStatus}
    assert counts["queued"] == 3
    assert counts["running"] == 0
    assert counts["done"] == 1
    assert counts["error"] == 1
    assert counts["retry_pending"] == 1


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


def test_queue_missing_issues_since_excludes_issues_discovered_earlier(session):
    """TASK-1361: poll's catch-up must not sweep in the back catalogue."""
    from datetime import datetime, timedelta

    repo = DownloadRepository(session)
    pub = _pub_with_issues(repo, ["new", "new"])
    session.commit()
    issues = list(repo.list_issues(publication_id=pub.id))
    older, newer = issues[0], issues[1]
    older.discovered_at = datetime(2020, 1, 1)
    newer.discovered_at = datetime(2026, 1, 1)
    session.commit()

    cutoff = datetime(2026, 1, 1) - timedelta(days=1)
    queued = repo.queue_missing_issues(pub.id, since=cutoff)
    session.commit()

    assert queued == 1
    assert repo.get_issue(newer.id).status == IssueStatus.QUEUED
    assert repo.get_issue(older.id).status == IssueStatus.NEW


def test_queue_missing_issues_since_none_still_queues_everything(session):
    """The explicit backfill button must keep reaching the whole backlog."""
    from datetime import datetime

    repo = DownloadRepository(session)
    pub = _pub_with_issues(repo, ["new", "new"])
    session.commit()
    issues = list(repo.list_issues(publication_id=pub.id))
    issues[0].discovered_at = datetime.min.replace(year=1901)
    session.commit()

    queued = repo.queue_missing_issues(pub.id, since=None)
    session.commit()

    assert queued == 2


def test_set_watched_stamps_watch_started_at(repo):
    repo.upsert_publication(_publication())
    repo.session.commit()

    repo.set_watched("KA", True)
    repo.session.commit()

    assert repo.get_publication("KA").watch_started_at is not None


def test_set_watched_false_leaves_watch_started_at_untouched(repo):
    repo.upsert_publication(_publication())
    repo.set_watched("KA", True)
    repo.session.commit()
    started_at = repo.get_publication("KA").watch_started_at

    repo.set_watched("KA", False)
    repo.session.commit()

    assert repo.get_publication("KA").watch_started_at == started_at


def test_queue_warn_threshold_bytes_default_is_5gib(repo):
    assert repo.queue_warn_threshold_bytes() == 5 * 1024**3


def test_queue_warn_threshold_bytes_setting_overrides_default(repo):
    repo.set_setting("queue_warn_threshold_bytes", "1000")
    repo.session.commit()

    assert repo.queue_warn_threshold_bytes() == 1000


def test_queue_warn_threshold_bytes_env_overrides_default(repo, monkeypatch):
    monkeypatch.setenv("FLIPP_QUEUE_WARN_THRESHOLD_BYTES", "2000")

    assert repo.queue_warn_threshold_bytes() == 2000


def test_queue_warn_threshold_bytes_setting_beats_env(repo, monkeypatch):
    monkeypatch.setenv("FLIPP_QUEUE_WARN_THRESHOLD_BYTES", "2000")
    repo.set_setting("queue_warn_threshold_bytes", "1000")
    repo.session.commit()

    assert repo.queue_warn_threshold_bytes() == 1000


def test_queue_warn_threshold_bytes_ignores_garbage_setting(repo):
    repo.set_setting("queue_warn_threshold_bytes", "not-a-number")
    repo.session.commit()

    assert repo.queue_warn_threshold_bytes() == 5 * 1024**3


# ---------------------------------------------------------------------------
# estimate_missing_download_size (TASK-1362)
# ---------------------------------------------------------------------------


def _pub_with_sized_issues(repo, specs: list[tuple[str, int | None]], code: str = "KA"):
    """Create a publication whose issues have the given (status, file_size).

    ``file_size`` may be ``None`` even for a ``done`` issue - old rows
    downloaded before this column existed.
    """
    db_pub = repo.upsert_publication(Publication(custom_code=code, name="Kalle Anka"))
    repo.session.flush()
    for i, (status, size) in enumerate(specs):
        issue = Issue(
            custom_code=f"{code}{i}", issue_name=f"Nr {i}", issue_date="2024-01-01"
        )
        db_issue, _ = repo.upsert_issue(issue, db_pub.id)
        db_issue.status = status
        db_issue.file_size = size
    repo.session.flush()
    return db_pub


def test_estimate_uses_publications_own_average(repo):
    pub = _pub_with_sized_issues(
        repo,
        [
            (IssueStatus.DONE, 100),
            (IssueStatus.DONE, 200),
            (IssueStatus.NEW, None),
            (IssueStatus.NEW, None),
        ],
    )

    estimate = repo.estimate_missing_download_size(pub.id)

    assert estimate.issue_count == 2
    assert estimate.basis == "publication"
    # Average of 100/200 is 150 bytes/issue, times 2 missing issues.
    assert estimate.estimated_bytes == 300


def test_estimate_falls_back_to_global_median_without_own_data(repo):
    other = _pub_with_sized_issues(
        repo, [(IssueStatus.DONE, 100), (IssueStatus.DONE, 300)], code="OT"
    )
    pub = _pub_with_sized_issues(
        repo, [(IssueStatus.NEW, None), (IssueStatus.NEW, None)], code="KA"
    )
    repo.session.commit()

    estimate = repo.estimate_missing_download_size(pub.id)

    assert other.id != pub.id
    assert estimate.issue_count == 2
    assert estimate.basis == "global"
    # Median of [100, 300] is 200 bytes/issue, times 2 missing issues.
    assert estimate.estimated_bytes == 400


def test_estimate_returns_none_bytes_when_no_size_data_exists_anywhere(repo):
    pub = _pub_with_sized_issues(
        repo, [(IssueStatus.NEW, None), (IssueStatus.ERROR, None)]
    )

    estimate = repo.estimate_missing_download_size(pub.id)

    assert estimate.issue_count == 2
    assert estimate.basis == "none"
    assert estimate.estimated_bytes is None


def test_estimate_zero_missing_issues_reports_zero_count(repo):
    pub = _pub_with_sized_issues(repo, [(IssueStatus.DONE, 100)])

    estimate = repo.estimate_missing_download_size(pub.id)

    assert estimate.issue_count == 0


def test_estimate_can_exclude_failed_issues(repo):
    pub = _pub_with_sized_issues(
        repo,
        [(IssueStatus.DONE, 100), (IssueStatus.NEW, None), (IssueStatus.ERROR, None)],
    )

    estimate = repo.estimate_missing_download_size(pub.id, include_failed=False)

    assert estimate.issue_count == 1


def test_mark_issue_done_stores_file_size(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.flush()
    db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    repo.session.flush()

    repo.mark_issue_done(db_issue.id, "/tmp/out.pdf", file_size=12345)
    repo.session.commit()

    assert repo.get_issue(db_issue.id).file_size == 12345


def test_mark_issue_done_without_size_leaves_it_unset(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.flush()
    db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    repo.session.flush()

    repo.mark_issue_done(db_issue.id, "/tmp/out.pdf")
    repo.session.commit()

    assert repo.get_issue(db_issue.id).file_size is None


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
    assert refreshed.file_size == target.stat().st_size
    assert report.orphan_files == []
    assert report.missing_files == []
    assert report.shared_files == []


def test_import_existing_reports_file_in_another_publications_folder(repo, tmp_path):
    actual_pub = _publication("A", "Publication A")
    expected_pub = _publication("B", "Publication B")
    repo.upsert_publication(actual_pub)
    db_expected_pub = repo.upsert_publication(expected_pub)
    db_issue, _ = repo.upsert_issue(expected_pub.issues[0], db_expected_pub.id)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    wrong_folder = storage.publication_folder(tmp_path, actual_pub)
    wrong_folder.mkdir(parents=True)
    misplaced = wrong_folder / storage.issue_filename(
        expected_pub, expected_pub.issues[0]
    )
    misplaced.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert report.backfilled == []
    assert report.orphan_files == []
    assert report.misplaced_files == [
        {
            "file_path": str(misplaced.relative_to(tmp_path)),
            "matching_issues": [
                {
                    "issue_id": db_issue.id,
                    "publication": expected_pub.name,
                    "issue_name": expected_pub.issues[0].issue_name,
                }
            ],
        }
    ]
    assert report.has_findings
    assert repo.get_issue(db_issue.id).status == IssueStatus.QUEUED
    assert repo.get_issue(db_issue.id).file_path is None


def test_import_existing_backfills_a_disambiguated_filename(repo, tmp_path):
    pub = Publication(
        custom_code="DUP",
        name="Duplicate",
        issues=[
            Issue(
                custom_code="duplicate-01",
                issue_name="Nr 1",
                issue_date="2024-01-01",
            ),
            Issue(
                custom_code="duplicate-02",
                issue_name="Nr 1",
                issue_date="2024-01-01",
            ),
        ],
    )
    db_pub = repo.upsert_publication(pub)
    db_issue, _ = repo.upsert_issue(pub.issues[1], db_pub.id)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    target = storage.issue_path(tmp_path, pub, pub.issues[1], disambiguate=True)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert [item["issue_id"] for item in report.backfilled] == [db_issue.id]
    assert repo.get_issue(db_issue.id).file_path == str(target.resolve())
    assert report.misplaced_files == []


def test_import_existing_reports_ambiguous_publication_folder(repo, tmp_path):
    first = _publication("A", "Foo/Bar")
    second = _publication("B", "Foo-Bar")
    repo.upsert_publication(first)
    db_second = repo.upsert_publication(second)
    db_issue, _ = repo.upsert_issue(second.issues[0], db_second.id)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    target = storage.issue_path(tmp_path, second, second.issues[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert report.backfilled == []
    assert report.orphan_files == []
    assert len(report.misplaced_files) == 1
    assert report.misplaced_files[0]["matching_issues"] == [
        {
            "issue_id": db_issue.id,
            "publication": second.name,
            "issue_name": second.issues[0].issue_name,
        }
    ]
    assert repo.get_issue(db_issue.id).status == IssueStatus.QUEUED


def test_import_existing_is_unambiguous_after_folder_names_are_separated(
    repo, tmp_path
):
    first = _publication("A", "Hjemmet")
    second = _publication("B", "Hjemmet")
    repo.upsert_publication(first)
    db_second = repo.upsert_publication(second)
    db_issue, _ = repo.upsert_issue(second.issues[0], db_second.id)
    repo.set_publication_folder_name("A", "Hjemmet (NO)", tmp_path)
    repo.set_publication_folder_name("B", "Hjemmet (DK)", tmp_path)
    repo.mark_issue_queued(db_issue.id)
    repo.session.commit()

    target = storage.issue_path(tmp_path, db_second, db_issue)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    report = repo.import_existing_files(tmp_path)
    repo.session.commit()

    assert [item["issue_id"] for item in report.backfilled] == [db_issue.id]
    assert report.misplaced_files == []
    assert repo.get_issue(db_issue.id).file_path == str(target.resolve())


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


def test_issues_needing_cover_backfill_finds_uncached_issues(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    db_issue_1, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    db_issue_2, _ = repo.upsert_issue(pub.issues[1], db_pub.id)
    repo.session.commit()

    missing = repo.issues_needing_cover_backfill(limit=10)

    assert {i.id for i in missing} == {db_issue_1.id, db_issue_2.id}


def test_issues_needing_cover_backfill_skips_already_cached(repo):
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    db_issue_1, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    db_issue_2, _ = repo.upsert_issue(pub.issues[1], db_pub.id)
    repo.session.commit()
    repo.set_issue_cover_cache(db_issue_1.id, "issue-KA-01.jpg")
    repo.session.commit()

    missing = repo.issues_needing_cover_backfill(limit=10)

    assert [i.id for i in missing] == [db_issue_2.id]


def test_issues_needing_cover_backfill_respects_limit_and_order(repo):
    """Newest issue (highest id) comes first - it's the one most likely
    to actually be on screen right now (TASK-1374)."""
    pub = _publication()
    db_pub = repo.upsert_publication(pub)
    repo.session.commit()
    db_issue_1, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
    db_issue_2, _ = repo.upsert_issue(pub.issues[1], db_pub.id)
    repo.session.commit()

    missing = repo.issues_needing_cover_backfill(limit=1)

    assert [i.id for i in missing] == [db_issue_2.id]


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


def test_import_records_size_for_already_downloaded_issues(session, tmp_path):
    """Issues downloaded before sizes were tracked must get one now.

    Without this the publication list shows "unknown" forever: the import
    skipped anything already marked done, which is exactly the set of
    issues missing a size (TASK-1362 landed after they were fetched).
    """
    from flipp_dl import storage
    from flipp_dl.models import Issue, Publication

    repo = DownloadRepository(session)
    pub = Publication(custom_code="KA", name="Kalle Anka")
    db_pub = repo.upsert_publication(pub)
    issue = Issue(custom_code="ka01", issue_name="Nr 1", issue_date="2024-01-01")
    db_issue, _ = repo.upsert_issue(issue, db_pub.id)

    target = storage.issue_path(tmp_path, pub, issue)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4\n" + b"x" * 500)

    repo.mark_issue_done(db_issue.id, str(target))
    db_issue.file_size = None  # as if downloaded before sizes existed
    session.flush()

    report = repo.import_existing_files(tmp_path)

    assert db_issue.file_size == target.stat().st_size
    assert len(report.sized) == 1
    # Status and timestamps are untouched - this only fills in the size.
    assert db_issue.status == "done"
    assert report.backfilled == []


def test_cover_backfill_prioritises_downloaded_issues(session):
    """A freshly downloaded issue must not queue behind the back catalogue.

    Ordering purely by discovery id put a just-downloaded old issue at
    the end of a 17660-row queue, which showed as blank covers in the
    dashboard's recent downloads.
    """
    from datetime import datetime

    from flipp_dl.models import Issue, Publication

    repo = DownloadRepository(session)
    db_pub = repo.upsert_publication(Publication(custom_code="KA", name="Kalle"))

    # Discovered first (lowest id) but downloaded just now.
    old_but_downloaded, _ = repo.upsert_issue(
        Issue(custom_code="old", issue_name="Nr 1", issue_date="2020-01-01"), db_pub.id
    )
    # Discovered later, never downloaded.
    for i in range(3):
        repo.upsert_issue(
            Issue(custom_code=f"new{i}", issue_name=f"Nr {i}", issue_date="2026-01-01"),
            db_pub.id,
        )
    repo.mark_issue_done(old_but_downloaded.id, "/output/KA/old.pdf")
    old_but_downloaded.downloaded_at = datetime(2026, 8, 20, 12, 0)
    old_but_downloaded.cover_cache_path = None
    session.flush()

    first = repo.issues_needing_cover_backfill(4)[0]
    assert first.id == old_but_downloaded.id


def test_import_existing_files_scans_primary_and_secondary_roots(repo, tmp_path):
    primary_root = tmp_path / "primary"
    secondary_root = tmp_path / "secondary"
    primary_pub = _publication("PRI", "Primary")
    secondary_pub = _publication("SEC", "Secondary")
    db_primary = repo.upsert_publication(primary_pub)
    db_secondary = repo.upsert_publication(secondary_pub)
    db_secondary.destination = "secondary"
    primary_issue, _ = repo.upsert_issue(primary_pub.issues[0], db_primary.id)
    secondary_issue, _ = repo.upsert_issue(secondary_pub.issues[0], db_secondary.id)
    repo.session.commit()

    primary_target = storage.issue_path(
        primary_root, primary_pub, primary_pub.issues[0]
    )
    secondary_target = storage.issue_path(
        secondary_root, db_secondary, secondary_pub.issues[0]
    )
    primary_target.parent.mkdir(parents=True)
    secondary_target.parent.mkdir(parents=True)
    primary_target.write_bytes(b"%PDF-1.4\n%primary\n")
    secondary_target.write_bytes(b"%PDF-1.4\n%secondary\n")

    report = repo.import_existing_files(primary_root, extra_roots=[secondary_root])
    repo.session.commit()

    assert {item["issue_id"] for item in report.backfilled} == {
        primary_issue.id,
        secondary_issue.id,
    }
    assert repo.get_issue(primary_issue.id).file_path == str(primary_target.resolve())
    assert repo.get_issue(secondary_issue.id).file_path == str(
        secondary_target.resolve()
    )
    assert report.missing_files == []


# ---------------------------------------------------------------------------
# Publications with no cover URL borrow their newest issue's cover (TASK-1461)
# ---------------------------------------------------------------------------


def test_a_publication_without_a_cover_url_borrows_its_newest_issue_cover(repo):
    """The unlisted publications never came from Flipp, so nothing fetches
    them a cover - the list would render them blank forever."""
    pub = Publication(custom_code="AS", name="Ankeborgs samlarpocket")
    db_pub = repo.upsert_publication(pub)
    for code, datum in (("a1", "2023-01-10"), ("a2", "2023-11-09")):
        db_issue, _ = repo.upsert_issue(
            Issue(custom_code=code, issue_name=datum, issue_date=datum), db_pub.id
        )
        repo.set_issue_cover_cache(db_issue.id, f"issue-{code}.jpg")

    assert repo.link_publication_covers_from_issues() == 1
    assert repo.get_publication("AS").cover_cache_path == "issue-a2.jpg"


def test_a_publication_with_its_own_cover_url_is_never_overwritten(repo):
    pub = Publication(
        custom_code="KA", name="Kalle Anka", cover_url="https://flipp/ka.jpg"
    )
    db_pub = repo.upsert_publication(pub)
    repo.set_publication_cover_cache(db_pub.id, "pub-KA.jpg", "https://flipp/ka.jpg")
    db_issue, _ = repo.upsert_issue(
        Issue(custom_code="ka1", issue_name="Nr 1", issue_date="2024-01-01"), db_pub.id
    )
    repo.set_issue_cover_cache(db_issue.id, "issue-ka1.jpg")

    assert repo.link_publication_covers_from_issues() == 0
    assert repo.get_publication("KA").cover_cache_path == "pub-KA.jpg"


def test_linking_follows_along_when_a_newer_issue_arrives(repo):
    """Re-running is cheap and keeps the publication on its newest issue."""
    db_pub = repo.upsert_publication(Publication(custom_code="AS", name="Ankeborg"))
    first, _ = repo.upsert_issue(
        Issue(custom_code="a1", issue_name="1", issue_date="2023-01-10"), db_pub.id
    )
    repo.set_issue_cover_cache(first.id, "issue-a1.jpg")
    repo.link_publication_covers_from_issues()

    # Running again with nothing new must not write anything.
    assert repo.link_publication_covers_from_issues() == 0

    later, _ = repo.upsert_issue(
        Issue(custom_code="a2", issue_name="2", issue_date="2023-11-09"), db_pub.id
    )
    repo.set_issue_cover_cache(later.id, "issue-a2.jpg")
    assert repo.link_publication_covers_from_issues() == 1
    assert repo.get_publication("AS").cover_cache_path == "issue-a2.jpg"


def test_a_publication_whose_issues_have_no_covers_is_left_alone(repo):
    db_pub = repo.upsert_publication(Publication(custom_code="AS", name="Ankeborg"))
    repo.upsert_issue(
        Issue(custom_code="a1", issue_name="1", issue_date="2023-01-10"), db_pub.id
    )
    assert repo.link_publication_covers_from_issues() == 0
    assert repo.get_publication("AS").cover_cache_path is None
