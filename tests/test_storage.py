from pathlib import Path

import pytest

from flipp_dl.models import Category, Issue, Publication
from flipp_dl.storage import (
    destination_root,
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


@pytest.mark.parametrize("value", ["CON", "con.pdf", "PRN.txt", "COM1", "LPT9.pdf"])
def test_safe_name_prefixes_windows_reserved_names(value):
    assert safe_name(value).startswith("_")


def test_safe_name_removes_trailing_dots_and_spaces():
    assert safe_name("Kalle Anka.  ") == "Kalle Anka"


def test_safe_name_uses_fallback_when_every_character_is_removed():
    assert safe_name("!?<>:|*") == "unnamed"
    assert safe_name("!?<>:|*.pdf") == "unnamed.pdf"


def test_safe_name_normalizes_equivalent_unicode_to_nfc():
    assert safe_name("Ra\u0308ven") == safe_name("Räven") == "Räven"


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


def test_publication_folder_uses_own_name_when_present():
    root = Path("/tmp/out")
    pub = _publication()
    pub.folder_name = "Kalle Anka (SE)"

    assert publication_folder(root, pub) == root / "Kalle Anka (SE)"


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


def test_issue_filename_can_be_disambiguated():
    """Two issues sharing name and date must not share a filename.

    Flipp publishes distinct issues with identical name and date, so
    the plain filename is not unique (TASK-1349).
    """
    from flipp_dl.models import Issue, Publication
    from flipp_dl.storage import issue_filename

    pub = Publication(custom_code="91", name="91:an")
    a = Issue(
        custom_code="ab2041c2-bb4d", issue_name="Nr 6 2022", issue_date="2022-02-25"
    )
    b = Issue(
        custom_code="6410d950-be8e", issue_name="Nr 6 2022", issue_date="2022-02-25"
    )

    assert issue_filename(pub, a) == issue_filename(pub, b)
    assert issue_filename(pub, a, disambiguate=True) != issue_filename(
        pub, b, disambiguate=True
    )
    # The plain name stays as it is, so existing files are not renamed.
    assert issue_filename(pub, a) == "91an - 2022-02-25 - Nr 6 2022.pdf"
    assert issue_filename(pub, a, disambiguate=True).endswith("(ab2041c2).pdf")


def test_issue_path_shortens_long_paths_with_a_stable_unique_hash(tmp_path):
    publication = Publication(custom_code="LONG", name="A" * 170)
    issue_a = Issue(
        custom_code="issue-a",
        issue_name="B" * 220 + "A",
        issue_date="2026-08-20",
    )
    issue_b = Issue(
        custom_code="issue-b",
        issue_name="B" * 220 + "B",
        issue_date="2026-08-20",
    )

    path_a = issue_path(tmp_path, publication, issue_a)
    path_b = issue_path(tmp_path, publication, issue_b)

    assert len(str(path_a.absolute())) <= 259
    assert len(str(path_b.absolute())) <= 259
    assert path_a != path_b
    assert path_a.suffix == ".pdf"


def test_long_disambiguated_filename_keeps_the_existing_suffix(tmp_path):
    publication = Publication(custom_code="LONG", name="A" * 170)
    issue = Issue(
        custom_code="ab2041c2-bb4d",
        issue_name="B" * 220,
        issue_date="2026-08-20",
    )

    path = issue_path(tmp_path, publication, issue, disambiguate=True)

    assert len(str(path.absolute())) <= 259
    assert path.name.endswith("(ab2041c2).pdf")


def test_destination_root_uses_primary_by_default():
    publication = _publication()

    assert destination_root(Path("/primary"), Path("/secondary"), publication) == Path(
        "/primary"
    )


def test_destination_root_uses_configured_secondary():
    publication = _publication()
    publication.destination = "secondary"

    assert destination_root(Path("/primary"), Path("/secondary"), publication) == Path(
        "/secondary"
    )


def test_destination_root_falls_back_when_secondary_is_not_configured():
    publication = _publication()
    publication.destination = "secondary"

    assert destination_root(Path("/primary"), None, publication) == Path("/primary")


# ---------------------------------------------------------------------------
# Issues named after nothing but their own date (TASK-1460)
# ---------------------------------------------------------------------------


def _issue(name: str, date: str = "2020-02-20") -> Issue:
    return Issue(custom_code="e1", issue_name=name, issue_date=date)


def _pub() -> Publication:
    return Publication(custom_code="91", name="91:an")


def test_an_issue_named_after_its_own_date_does_not_repeat_it():
    """PageSuite names discovered editions after their date, in another
    notation - pasting that after the ISO date gave the same date twice."""
    assert issue_filename(_pub(), _issue("20/02/2020")) == "91an - 2020-02-20.pdf"


def test_an_issue_with_no_name_gets_no_dangling_separator():
    assert issue_filename(_pub(), _issue("")) == "91an - 2020-02-20.pdf"


def test_a_real_issue_name_is_kept_even_when_it_contains_a_date():
    """ "Nr 3 2020" shares digits with the date - it must survive."""
    assert (
        issue_filename(_pub(), _issue("Nr 3 2020"))
        == "91an - 2020-02-20 - Nr 3 2020.pdf"
    )
    assert (
        issue_filename(_pub(), _issue("Nr 8/9 2023", "2023-03-24"))
        == "91an - 2023-03-24 - Nr 8-9 2023.pdf"
    )


def test_a_date_named_issue_can_still_be_disambiguated():
    """Two issues on the same date still get separate files (TASK-1349)."""
    plain = issue_filename(_pub(), _issue("20/02/2020"))
    unique = issue_filename(_pub(), _issue("20/02/2020"), disambiguate=True)
    assert plain != unique
    assert unique.startswith("91an - 2020-02-20 (")
