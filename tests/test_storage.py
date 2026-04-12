from pathlib import Path

from flipp_dl.models import Category, Issue, Publication
from flipp_dl.storage import (
    issue_filename,
    issue_path,
    publication_folder,
    safe_name,
)


def test_safe_name_strips_slashes_and_ampersands():
    assert safe_name("Foo/Bar & Baz") == "Foo-Bar och Baz"


def test_safe_name_keeps_swedish_letters():
    assert safe_name("Kalle Anka & Co åäöÅÄÖ") == "Kalle Anka och Co åäöÅÄÖ"


def test_safe_name_drops_disallowed_chars():
    assert safe_name("Foo!?<>:|*Bar") == "FooBar"


def test_safe_name_keeps_parentheses_and_dots():
    assert safe_name("Nr.1 (2024).pdf") == "Nr.1 (2024).pdf"


def _publication() -> Publication:
    return Publication(
        custom_code="KA",
        name="Kalle Anka & Co",
        categories=[Category(id=52, name="Serietidningar")],
        issues=[
            Issue(
                custom_code="ISSUE-1",
                issue_name="Nr 1",
                issue_date="2024-01-01",
            )
        ],
    )


def test_publication_folder_uses_safe_name():
    root = Path("/tmp/out")
    pub = _publication()
    assert publication_folder(root, pub) == root / "Kalle Anka och Co"


def test_issue_filename_combines_fields():
    pub = _publication()
    issue = pub.issues[0]
    assert issue_filename(pub, issue) == "Kalle Anka och Co - 2024-01-01 - Nr 1.pdf"


def test_issue_path_joins_folder_and_filename():
    root = Path("/tmp/out")
    pub = _publication()
    issue = pub.issues[0]
    assert issue_path(root, pub, issue) == (
        root / "Kalle Anka och Co" / "Kalle Anka och Co - 2024-01-01 - Nr 1.pdf"
    )
