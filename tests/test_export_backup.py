"""Tests for backing up and restoring pubids/eids (TASK-1438 backup)."""

from __future__ import annotations

import json

from flipp_dl.db.repository import DownloadRepository
from flipp_dl.db.session import make_session_factory
from flipp_dl.models import Issue, Publication


def _seed(db):
    factory = make_session_factory(str(db))
    with factory() as s:
        repo = DownloadRepository(s)
        pub = repo.upsert_publication(Publication(custom_code="pub-1", name="Bilar"))
        repo.upsert_issue(
            Issue(custom_code="e1", issue_name="Nr 1", issue_date="2026-01-01"), pub.id
        )
        repo.upsert_issue(
            Issue(custom_code="e2", issue_name="Nr 2", issue_date="2026-01-15"), pub.id
        )
        delisted = repo.upsert_publication(
            Publication(custom_code="pub-2", name="Båtnytt")
        )
        repo.backfill_publication_codes({"pub-1": "SE-CAR"})
        from datetime import datetime, timezone

        delisted.delisted_at = datetime.now(timezone.utc)
        s.commit()


def test_export_then_import_backup_round_trips(tmp_path, capsys):
    from flipp_dl.cli import main

    src_db = tmp_path / "src.db"
    _seed(src_db)
    backup = tmp_path / "backup.json"

    rc = main(["--db", str(src_db), "--export-codes", str(backup)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 publications and 2 issue codes" in out

    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert {p["customPublicationCode"] for p in payload["publications"]} == {
        "pub-1",
        "pub-2",
    }
    assert {i["issue_code"] for i in payload["issues"]} == {"e1", "e2"}

    # Restore into a fresh database.
    dst_db = tmp_path / "dst.db"
    rc = main(["--db", str(dst_db), "--import-backup", str(backup)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Restored 2 publications and 2 issue codes" in out

    factory = make_session_factory(str(dst_db))
    with factory() as s:
        repo = DownloadRepository(s)
        bilar = repo.get_publication("pub-1")
        assert bilar.publication_code == "SE-CAR"
        assert {i.custom_code for i in repo.list_issues(publication_id=bilar.id)} == {
            "e1",
            "e2",
        }
        batnytt = repo.get_publication("pub-2")
        assert batnytt.delisted_at is not None


def test_import_backup_missing_file_errors(tmp_path):
    from flipp_dl.cli import main

    rc = main(
        ["--db", str(tmp_path / "x.db"), "--import-backup", str(tmp_path / "no.json")]
    )
    assert rc == 2
