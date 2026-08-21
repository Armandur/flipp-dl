"""Tests for importing shadow issue codes outside the listing API (TASK-1437)."""

from __future__ import annotations

import json

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import make_session_factory
from flipp_dl.models import Publication


class _FakeMeta:
    def __init__(self, publication_guid, edition_name, iso_date):
        self.publication_guid = publication_guid
        self.edition_name = edition_name
        self.iso_date = iso_date


def _seed_pub(db, code, name="Bilar"):
    factory = make_session_factory(str(db))
    with factory() as s:
        DownloadRepository(s).upsert_publication(Publication(custom_code=code, name=name))
        s.commit()


def test_import_editions_attaches_shadow_eids(tmp_path, capsys, monkeypatch):
    from flipp_dl import cli

    db = tmp_path / "flipp.db"
    _seed_pub(db, "pub-1")

    editions_file = tmp_path / "olistade.json"
    editions_file.write_text(
        json.dumps(
            {
                "found_unlisted_issues": [
                    {"issue_code": "shadow-1", "publication_code": "pub-1"},
                    {"issue_code": "shadow-2", "publication_code": None},  # resolved via replica
                    {"issue_code": "dead-1", "publication_code": "pub-1"},
                    {"issue_code": "orphan-1", "publication_code": "unknown-pub"},
                ]
            }
        ),
        encoding="utf-8",
    )

    resolved = {
        "shadow-1": _FakeMeta("pub-1", "01/02/2026", "2026-02-01"),
        "shadow-2": _FakeMeta("pub-1", "08/02/2026", "2026-02-08"),
        "dead-1": None,
        "orphan-1": _FakeMeta("still-unknown", "01/01/2026", "2026-01-01"),
    }

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def fetch_edition_metadata(self, eid):
            return resolved.get(eid)

    monkeypatch.setattr("flipp_dl.pagesuite.PageSuiteClient", _FakeClient)

    rc = cli.main(["--db", str(db), "--import-editions", str(editions_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 shadow editions imported" in out
    assert "1 dead eids" in out
    assert "1 with no known publication" in out

    factory = make_session_factory(str(db))
    with factory() as s:
        repo = DownloadRepository(s)
        pub = repo.get_publication("pub-1")
        issues = repo.list_issues(publication_id=pub.id)
        codes = {i.custom_code for i in issues}
        assert codes == {"shadow-1", "shadow-2"}
        by_code = {i.custom_code: i for i in issues}
        assert by_code["shadow-1"].issue_date == "2026-02-01"


def test_import_editions_missing_file_errors(tmp_path):
    from flipp_dl.cli import main

    rc = main(
        ["--db", str(tmp_path / "x.db"), "--import-editions", str(tmp_path / "nope.json")]
    )
    assert rc == 2
