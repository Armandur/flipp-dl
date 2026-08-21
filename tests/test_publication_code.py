"""Tests for publication codes on stored publications."""

import pytest
from sqlalchemy.orm import Session

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
        categories=[Category(id=52, name="Serietidningar")],
        issues=[
            Issue(custom_code=f"{code}-01", issue_name="Nr 1", issue_date="2024-01-01")
        ],
    )


def test_publication_code_defaults_to_none(repo: DownloadRepository) -> None:
    publication = repo.upsert_publication(_publication())
    repo.session.commit()

    assert publication.publication_code is None


def test_backfill_publication_codes_updates_matching_rows_and_returns_count(
    repo: DownloadRepository,
) -> None:
    repo.upsert_publication(_publication("pagesuite-dk", "Hjemmet"))
    repo.upsert_publication(_publication("pagesuite-no", "Hjemmet"))
    repo.session.commit()

    updated = repo.backfill_publication_codes(
        {
            "pagesuite-dk": "DK-HJM",
            "pagesuite-no": "NO-HJE",
            "not-in-database": "SE-UVH",
        }
    )
    repo.session.commit()

    assert updated == 2
    assert repo.get_publication("pagesuite-dk").publication_code == "DK-HJM"
    assert repo.get_publication("pagesuite-no").publication_code == "NO-HJE"


def test_backfill_publication_codes_is_idempotent(repo: DownloadRepository) -> None:
    repo.upsert_publication(_publication("pagesuite-se", "Uti vår hage"))
    repo.session.commit()
    mapping = {"pagesuite-se": "SE-UVH"}

    assert repo.backfill_publication_codes(mapping) == 1
    repo.session.commit()
    assert repo.backfill_publication_codes(mapping) == 0


def _prepare_watched_publications(
    repo: DownloadRepository,
    *,
    first_name: str = "Hjemmet Norge",
    second_name: str = "Hjemmet Danmark",
) -> None:
    first_publication = _publication("pagesuite-no", first_name)
    second_publication = _publication("pagesuite-dk", second_name)
    first = repo.upsert_publication(first_publication)
    second = repo.upsert_publication(second_publication)
    repo.upsert_issue(first_publication.issues[0], first.id)
    repo.upsert_issue(second_publication.issues[0], second.id)
    first.watched = True
    second.watched = True
    repo.session.commit()


def test_sync_disambiguates_empty_folder_collision_with_publication_codes(
    repo: DownloadRepository,
) -> None:
    _prepare_watched_publications(repo)
    repo.backfill_publication_codes(
        {"pagesuite-no": "NO-HJE", "pagesuite-dk": "DK-HJM"}
    )

    repo.sync_publications(
        [
            _publication("pagesuite-no", "Hjemmet"),
            _publication("pagesuite-dk", "Hjemmet"),
        ]
    )
    repo.session.commit()

    norwegian = repo.get_publication("pagesuite-no")
    danish = repo.get_publication("pagesuite-dk")
    assert norwegian.folder_name == "Hjemmet (NO-HJE)"
    assert danish.folder_name == "Hjemmet (DK-HJM)"
    assert norwegian.watched is True
    assert danish.watched is True


def test_sync_pauses_folder_collision_when_one_publication_has_downloads(
    repo: DownloadRepository,
) -> None:
    _prepare_watched_publications(repo)
    repo.backfill_publication_codes(
        {"pagesuite-no": "NO-HJE", "pagesuite-dk": "DK-HJM"}
    )
    norwegian = repo.get_publication("pagesuite-no")
    repo.mark_issue_done(norwegian.issues[0].id, "/output/Hjemmet/Nr 1.pdf")
    repo.session.commit()

    repo.sync_publications(
        [
            _publication("pagesuite-no", "Hjemmet"),
            _publication("pagesuite-dk", "Hjemmet"),
        ]
    )
    repo.session.commit()

    norwegian = repo.get_publication("pagesuite-no")
    danish = repo.get_publication("pagesuite-dk")
    assert norwegian.folder_name is None
    assert danish.folder_name is None
    assert norwegian.watched is False
    assert danish.watched is False


def test_sync_pauses_folder_collision_when_publication_code_is_missing(
    repo: DownloadRepository,
) -> None:
    _prepare_watched_publications(repo)
    repo.backfill_publication_codes({"pagesuite-no": "NO-HJE"})

    repo.sync_publications(
        [
            _publication("pagesuite-no", "Hjemmet"),
            _publication("pagesuite-dk", "Hjemmet"),
        ]
    )
    repo.session.commit()

    norwegian = repo.get_publication("pagesuite-no")
    danish = repo.get_publication("pagesuite-dk")
    assert norwegian.folder_name is None
    assert danish.folder_name is None
    assert norwegian.watched is False
    assert danish.watched is False


def test_sync_never_replaces_an_existing_folder_name(
    repo: DownloadRepository,
) -> None:
    _prepare_watched_publications(repo, second_name="Hjemmet")
    repo.backfill_publication_codes(
        {"pagesuite-no": "NO-HJE", "pagesuite-dk": "DK-HJM"}
    )
    danish = repo.get_publication("pagesuite-dk")
    danish.folder_name = "Hjemmet"
    repo.session.commit()

    repo.sync_publications(
        [
            _publication("pagesuite-no", "Hjemmet"),
            _publication("pagesuite-dk", "Hjemmet"),
        ]
    )
    repo.session.commit()

    norwegian = repo.get_publication("pagesuite-no")
    danish = repo.get_publication("pagesuite-dk")
    assert norwegian.folder_name == "Hjemmet (NO-HJE)"
    assert danish.folder_name == "Hjemmet"
    assert norwegian.watched is True
    assert danish.watched is True


def test_sync_does_not_set_folder_names_without_a_collision(
    repo: DownloadRepository,
) -> None:
    _prepare_watched_publications(repo)
    repo.backfill_publication_codes(
        {"pagesuite-no": "NO-HJE", "pagesuite-dk": "DK-HJM"}
    )

    repo.sync_publications(
        [
            _publication("pagesuite-no", "Hjemmet Norge"),
            _publication("pagesuite-dk", "Hjemmet Danmark"),
        ]
    )
    repo.session.commit()

    norwegian = repo.get_publication("pagesuite-no")
    danish = repo.get_publication("pagesuite-dk")
    assert norwegian.folder_name is None
    assert danish.folder_name is None
    assert norwegian.watched is True
    assert danish.watched is True
