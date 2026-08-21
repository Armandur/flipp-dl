"""Tests for importing unlisted publications as delisted (TASK-1442)."""

from __future__ import annotations

import json

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import make_session_factory


def _write(path, entries):
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_import_catalog_also_imports_unlisted_companion_as_delisted(tmp_path, capsys):
    from flipp_dl.cli import main

    catalog = tmp_path / "alla-publikationer.json"
    companion = tmp_path / "olistade-publikationer.json"
    _write(catalog, [{"name": "Bilar", "customPublicationCode": "listed-1"}])
    _write(
        companion,
        [
            {"name": "Båtnytt", "customPublicationCode": "unlisted-1"},
            {"name": "Djurliv", "customPublicationCode": "unlisted-2"},
        ],
    )
    db = tmp_path / "flipp.db"
    rc = main(["--db", str(db), "--import-catalog", str(catalog)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 publications added" in out
    assert "2 added as delisted" in out

    factory = make_session_factory(str(db))
    with factory() as s:
        repo = DownloadRepository(s)
        listed = repo.get_publication("listed-1")
        unlisted = repo.get_publication("unlisted-1")
        assert listed.delisted_at is None
        assert unlisted.delisted_at is not None
        assert unlisted.name == "Båtnytt"


def test_import_catalog_without_companion_still_works(tmp_path, capsys):
    from flipp_dl.cli import main

    catalog = tmp_path / "alla-publikationer.json"
    _write(catalog, [{"name": "Bilar", "customPublicationCode": "listed-1"}])
    db = tmp_path / "flipp.db"
    rc = main(["--db", str(db), "--import-catalog", str(catalog)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 publications added" in out
    # No companion -> no unlisted line.
    assert "delisted" not in out


def test_real_docs_files_give_118_publications(tmp_path):
    """The shipped docs files import to 91 listed + 27 delisted (TASK-1442)."""
    from pathlib import Path

    from flipp_dl.cli import main

    repo_root = Path(__file__).resolve().parent.parent
    catalog = repo_root / "docs" / "alla-publikationer.json"
    companion = repo_root / "docs" / "olistade-publikationer.json"
    if not (catalog.exists() and companion.exists()):
        import pytest

        pytest.skip("docs catalogue files not present")
    db = tmp_path / "flipp.db"
    rc = main(["--db", str(db), "--import-catalog", str(catalog)])
    assert rc == 0
    factory = make_session_factory(str(db))
    with factory() as s:
        repo = DownloadRepository(s)
        all_pubs = repo.list_publications()
        delisted = [p for p in all_pubs if p.delisted_at is not None]
        assert len(all_pubs) == 118
        assert len(delisted) == 27
