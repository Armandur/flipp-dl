from flipp_dl.cli import build_parser, filter_publications
from flipp_dl.models import Category, Issue, Publication


def _pub(code: str, name: str, *, cats: list[int], issues: int = 1) -> Publication:
    return Publication(
        custom_code=code,
        name=name,
        categories=[Category(id=c, name=f"Cat{c}") for c in cats],
        issues=[
            Issue(custom_code=f"{code}-{i}", issue_name=f"Nr {i}", issue_date="2024")
            for i in range(issues)
        ],
    )


def test_filter_by_category_single():
    pubs = [
        _pub("A", "A", cats=[52]),
        _pub("B", "B", cats=[7]),
        _pub("C", "C", cats=[52, 7]),
    ]
    result = filter_publications(pubs, category_ids=[52])
    assert [p.custom_code for p in result] == ["A", "C"]


def test_filter_by_category_multiple():
    pubs = [
        _pub("A", "A", cats=[52]),
        _pub("B", "B", cats=[7]),
        _pub("C", "C", cats=[8]),
    ]
    result = filter_publications(pubs, category_ids=[52, 7])
    assert [p.custom_code for p in result] == ["A", "B"]


def test_filter_by_publication_codes_overrides_category():
    pubs = [
        _pub("A", "A", cats=[52]),
        _pub("B", "B", cats=[52]),
        _pub("C", "C", cats=[7]),
    ]
    result = filter_publications(pubs, category_ids=[52], publication_codes=["C"])
    assert [p.custom_code for p in result] == ["C"]


def test_filter_no_filter_returns_all():
    pubs = [_pub("A", "A", cats=[52]), _pub("B", "B", cats=[7])]
    result = filter_publications(pubs)
    assert [p.custom_code for p in result] == ["A", "B"]


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.token is None
    assert args.category is None
    assert args.publication is None
    assert args.list_categories is False
    assert args.list_publications is False
    assert args.workers == 4
    assert args.no_skip_existing is False
    assert args.verbose == 0


def test_parser_accepts_multiple_categories_and_publications():
    args = build_parser().parse_args(
        [
            "--category",
            "52",
            "--category",
            "7",
            "--publication",
            "KA",
            "--workers",
            "8",
            "-vv",
        ]
    )
    assert args.category == [52, 7]
    assert args.publication == ["KA"]
    assert args.workers == 8
    assert args.verbose == 2


def test_parser_accepts_import_existing_flag():
    args = build_parser().parse_args(["--import-existing"])
    assert args.import_existing is True


# ---------------------------------------------------------------------------
# --import-existing (TASK-1283) – must never touch the network
# ---------------------------------------------------------------------------


def test_main_import_existing_runs_without_a_token(tmp_path, monkeypatch, capsys):
    """A missing FLIPP_TOKEN must not block --import-existing."""
    from flipp_dl.cli import main

    monkeypatch.delenv("FLIPP_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "--import-existing",
            "--db",
            str(tmp_path / "flipp.db"),
            "--output",
            str(tmp_path / "Output"),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Backfilled 0 issue(s)" in out
    assert "Orphan file(s)" in out
    assert "shared by more than one issue" in out


def test_main_import_existing_backfills_a_file_already_on_disk(
    tmp_path, monkeypatch, capsys
):
    from flipp_dl import storage
    from flipp_dl.cli import main
    from flipp_dl.db.repository import DownloadRepository
    from flipp_dl.db.session import get_session, make_session_factory

    monkeypatch.delenv("FLIPP_TOKEN", raising=False)

    db_path = tmp_path / "flipp.db"
    output = tmp_path / "Output"
    pub = _pub("KA", "Kalle Anka", cats=[52], issues=1)

    factory = make_session_factory(db_path)
    with get_session(factory) as session:
        repo = DownloadRepository(session)
        db_pub = repo.upsert_publication(pub)
        db_issue, _ = repo.upsert_issue(pub.issues[0], db_pub.id)
        repo.mark_issue_queued(db_issue.id)
        issue_id = db_issue.id

    target = storage.issue_path(output, pub, pub.issues[0])
    target.parent.mkdir(parents=True)
    target.write_bytes(b"%PDF-1.4\n%dummy\n")

    exit_code = main(
        ["--import-existing", "--db", str(db_path), "--output", str(output)]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Backfilled 1 issue(s)" in out

    with get_session(factory) as session:
        repo = DownloadRepository(session)
        assert repo.get_issue(issue_id).status == "done"
