"""Filename sanitization and output path helpers."""

from __future__ import annotations

import string
from pathlib import Path

from .models import Issue, Publication

_VALID_CHARS = frozenset("-_.()åäöÅÄÖ " + string.ascii_letters + string.digits)


def safe_name(value: str) -> str:
    """Return a filesystem-safe version of *value*.

    Slashes become dashes, ampersands become "och", and any character
    outside a small whitelist is dropped.
    """
    value = value.replace("/", "-").replace("&", "och")
    return "".join(c for c in value if c in _VALID_CHARS)


def publication_folder(output_root: Path, publication: Publication) -> Path:
    return output_root / safe_name(publication.name)


def issue_filename(publication: Publication, issue: Issue) -> str:
    return safe_name(
        f"{publication.name} - {issue.issue_date} - {issue.issue_name}.pdf"
    )


def issue_path(output_root: Path, publication: Publication, issue: Issue) -> Path:
    return publication_folder(output_root, publication) / issue_filename(
        publication, issue
    )
