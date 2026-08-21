"""Tests for edition discovery + catalogue import (TASK-1439)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from flipp_dl.db.models import DbIssue, IssueStatus
from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import make_session_factory
from flipp_dl.models import Issue, Publication


@pytest.fixture()
def session() -> Session:
    factory = make_session_factory(":memory:")
    with factory() as s:
        yield s


@pytest.fixture()
def repo(session: Session) -> DownloadRepository:
    return DownloadRepository(session)


def _pub(repo: DownloadRepository, code="PUB", name="Bilar"):
    return repo.upsert_publication(Publication(custom_code=code, name=name))


# ---------------------------------------------------------------------------
# repository.discover_editions
# ---------------------------------------------------------------------------


def test_discover_editions_adds_new_and_returns_them(repo):
    db_pub = _pub(repo)
    editions = [
        Issue(custom_code="e1", issue_name="Nr 1", issue_date="2026-01-01"),
        Issue(custom_code="e2", issue_name="Nr 2", issue_date="2026-01-15"),
    ]
    new = repo.discover_editions(db_pub.id, editions)
    assert {i.custom_code for i in new} == {"e1", "e2"}
    stored = repo.list_issues(publication_id=db_pub.id)
    assert {i.custom_code for i in stored} == {"e1", "e2"}
    assert all(i.status == IssueStatus.NEW for i in stored)


def test_discover_editions_is_idempotent(repo):
    db_pub = _pub(repo)
    editions = [Issue(custom_code="e1", issue_name="Nr 1", issue_date="2026-01-01")]
    repo.discover_editions(db_pub.id, editions)
    # Second run with the same edition returns nothing new and adds no rows.
    new = repo.discover_editions(db_pub.id, editions)
    assert new == []
    assert len(repo.list_issues(publication_id=db_pub.id)) == 1


def test_discover_editions_only_returns_the_newly_created_one(repo):
    db_pub = _pub(repo)
    repo.discover_editions(
        db_pub.id, [Issue(custom_code="e1", issue_name="Nr 1", issue_date="")]
    )
    new = repo.discover_editions(
        db_pub.id,
        [
            Issue(custom_code="e1", issue_name="Nr 1", issue_date=""),
            Issue(custom_code="e2", issue_name="Nr 2", issue_date=""),
        ],
    )
    assert {i.custom_code for i in new} == {"e2"}


def test_discover_editions_never_delists_missing_issues(repo, session):
    """The PageSuite list is a superset; a missing issue must survive.

    If discovery delisted like sync_publications would, an issue the
    Flipp API lists but PageSuite happens to omit in one call would get
    buried. This asserts delisted_at stays None - remove the "never
    delist" guard and this fails.
    """
    db_pub = _pub(repo)
    # Two issues exist (e.g. from an earlier API sync).
    repo.upsert_issue(Issue(custom_code="e1", issue_name="1", issue_date=""), db_pub.id)
    repo.upsert_issue(Issue(custom_code="e2", issue_name="2", issue_date=""), db_pub.id)
    session.flush()
    # Discovery sees only e1 this time.
    repo.discover_editions(
        db_pub.id, [Issue(custom_code="e1", issue_name="1", issue_date="")]
    )
    e2 = session.scalar(select(DbIssue).where(DbIssue.custom_code == "e2"))
    assert e2.delisted_at is None


# ---------------------------------------------------------------------------
# CLI: --import-catalog
# ---------------------------------------------------------------------------


def test_import_catalog_adds_publications(tmp_path, capsys):
    from flipp_dl.cli import main

    db = tmp_path / "flipp.db"
    catalog = tmp_path / "cat.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "name": "Bilar",
                    "customPublicationCode": "aaaa",
                    "publicationCode": "SE-CAR",
                },
                {
                    "name": "91:an",
                    "customPublicationCode": "bbbb",
                    "publicationCode": "SE-NIT",
                },
                {"name": "no code"},  # skipped
            ]
        ),
        encoding="utf-8",
    )
    rc = main(["--db", str(db), "--import-catalog", str(catalog)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 publications added" in out

    factory = make_session_factory(str(db))
    with factory() as s:
        repo = DownloadRepository(s)
        assert repo.get_publication("aaaa").name == "Bilar"
        assert repo.get_publication("bbbb").name == "91:an"


def test_import_catalog_missing_file_errors(tmp_path):
    from flipp_dl.cli import main

    rc = main(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--import-catalog",
            str(tmp_path / "nope.json"),
        ]
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# CLI: --discover-editions (network mocked)
# ---------------------------------------------------------------------------


def test_discover_editions_cli(tmp_path, capsys, monkeypatch):
    from flipp_dl import cli

    db = tmp_path / "flipp.db"
    factory = make_session_factory(str(db))
    with factory() as s:
        _pub(DownloadRepository(s), code="pubguid-1", name="Bilar")
        s.commit()

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def fetch_editions(self, guid, maxnumber=5000):
            assert guid == "pubguid-1"
            return [
                Issue(custom_code="e1", issue_name="Nr 1", issue_date="2026-01-01"),
                Issue(custom_code="e2", issue_name="Nr 2", issue_date="2026-01-15"),
            ]

    monkeypatch.setattr(cli, "_run_discover_editions", cli._run_discover_editions)
    monkeypatch.setattr("flipp_dl.pagesuite.PageSuiteClient", _FakeClient)

    rc = cli.main(["--db", str(db), "--discover-editions"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 new editions" in out

    with factory() as s:
        repo = DownloadRepository(s)
        pub = repo.get_publication("pubguid-1")
        assert len(repo.list_issues(publication_id=pub.id)) == 2
