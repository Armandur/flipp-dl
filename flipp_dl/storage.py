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


def issue_filename(
    publication: Publication, issue: Issue, *, disambiguate: bool = False
) -> str:
    """Filename for one issue, optionally made unique.

    Publication, date and issue name are not unique on their own: Flipp
    publishes distinct issues that share all three (TASK-1349). Passing
    *disambiguate* appends part of the issue code so the second issue
    gets a file of its own instead of silently reusing the first one's.
    """
    stem = f"{publication.name} - {issue.issue_date} - {issue.issue_name}"
    if disambiguate:
        stem = f"{stem} ({issue.custom_code[:8]})"
    return safe_name(f"{stem}.pdf")


def issue_path(
    output_root: Path,
    publication: Publication,
    issue: Issue,
    *,
    disambiguate: bool = False,
) -> Path:
    return publication_folder(output_root, publication) / issue_filename(
        publication, issue, disambiguate=disambiguate
    )


def resolve_safe_path(output_root: Path, candidate: str | Path | None) -> Path | None:
    """Resolve *candidate* relative to *output_root* and confirm containment.

    Accepts either an absolute path (e.g. the ``file_path`` stored in the DB)
    or a relative path (e.g. a library URL segment). Returns the resolved
    :class:`Path` only if it points to an existing regular file that lives
    under *output_root* - otherwise ``None``. This guards against
    path-traversal (``../../etc/passwd``) and stale entries pointing at
    files that have been removed from disk.

    Shared by the web layer (serving/deleting a file) and the disk-import
    reconciler (TASK-1283) - both need the same guarantee: never act on a
    path that resolves outside the managed output tree.
    """
    if not candidate:
        return None
    try:
        root = Path(output_root).resolve()
        raw = Path(candidate)
        resolved = (raw if raw.is_absolute() else (root / raw)).resolve()
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved
