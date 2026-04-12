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
