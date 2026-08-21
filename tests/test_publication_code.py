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
