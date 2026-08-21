"""Filename sanitization and output path helpers."""

from __future__ import annotations

import string
import unicodedata
from hashlib import sha256
from pathlib import Path

from .models import Issue, Publication

_VALID_CHARS = frozenset("-_.()åäöÅÄÖ " + string.ascii_letters + string.digits)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_WINDOWS_MAX_PATH = 259  # 260 including the terminating null character
_MAX_COMPONENT_LENGTH = 255
_HASH_LENGTH = 12
_EMPTY_NAME = "unnamed"


def safe_name(value: str) -> str:
    """Return a filesystem-safe version of *value*.

    Slashes become dashes, ampersands become "och", and any character
    outside a small whitelist is dropped.
    """
    value = unicodedata.normalize("NFC", value)
    value = value.replace("/", "-").replace("&", "och")
    value = "".join(c for c in value if c in _VALID_CHARS).rstrip(" .")
    if not value:
        value = _EMPTY_NAME
    elif value.startswith("."):
        # If the stem was entirely filtered out, preserve a remaining
        # extension without producing an extension-only filename.
        value = f"{_EMPTY_NAME}{value}"

    # Windows reserves these device names case-insensitively, even when
    # followed by an extension (for example CON.pdf).
    base_name = value.split(".", 1)[0].rstrip(" ").upper()
    if base_name in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value


def _shorten_component(value: str, max_length: int, *, protected_tail: str = "") -> str:
    """Shorten one path component and retain uniqueness with a hash."""
    if len(value) <= max_length:
        return value

    digest = sha256(value.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    marker = f" ({digest})"
    prefix_length = max_length - len(marker) - len(protected_tail)
    if prefix_length < 1:
        raise ValueError("Output path is too long for a safe filename")
    prefix = value[:prefix_length].rstrip(" .") or _EMPTY_NAME
    return f"{prefix}{marker}{protected_tail}"


def _disambiguation_tail(issue: Issue, disambiguate: bool) -> str:
    if not disambiguate:
        return ".pdf"
    return safe_name(f" ({issue.custom_code[:8]}).pdf")


def publication_folder(output_root: Path, publication: Publication) -> Path:
    # Persisted publications expose folder_name directly. Download workers
    # detach them into the domain model first, where the DB model's str
    # subclass carries the same value on publication.name.
    folder_name = getattr(publication, "folder_name", None) or getattr(
        publication.name, "folder_name", None
    )
    folder = _shorten_component(
        safe_name(folder_name or publication.name), _MAX_COMPONENT_LENGTH
    )
    return output_root / folder


def destination_root(
    primary: Path, secondary: Path | None, publication: Publication
) -> Path:
    """Return the configured output root for *publication*.

    A detached database publication carries its destination on the same
    string subclass that already preserves ``folder_name``.
    """
    destination = getattr(publication, "destination", None) or getattr(
        publication.name, "destination", None
    )
    if destination == "secondary" and secondary is not None:
        return Path(secondary)
    return Path(primary)


def issue_filename(
    publication: Publication, issue: Issue, *, disambiguate: bool = False
) -> str:
    """Filename for one issue, optionally made unique.

    Publication, date and issue name are not unique on their own: Flipp
    publishes distinct issues that share all three (TASK-1349). Passing
    *disambiguate* appends part of the issue code so the second issue
    gets a file of its own instead of silently reusing the first one's.
    """
    stem = safe_name(f"{publication.name} - {issue.issue_date} - {issue.issue_name}")
    tail = _disambiguation_tail(issue, disambiguate)
    filename = safe_name(f"{stem}{tail}")
    return _shorten_component(filename, _MAX_COMPONENT_LENGTH, protected_tail=tail)


def issue_path(
    output_root: Path,
    publication: Publication,
    issue: Issue,
    *,
    disambiguate: bool = False,
) -> Path:
    folder = publication_folder(output_root, publication)
    filename = issue_filename(publication, issue, disambiguate=disambiguate)
    candidate = folder / filename
    if len(str(candidate.absolute())) <= _WINDOWS_MAX_PATH:
        return candidate

    tail = _disambiguation_tail(issue, disambiguate)
    filename_budget = _WINDOWS_MAX_PATH - len(str(folder.absolute())) - 1
    minimum_filename_length = 1 + len(f" ({'0' * _HASH_LENGTH})") + len(tail)
    filename = _shorten_component(
        filename,
        max(filename_budget, minimum_filename_length),
        protected_tail=tail,
    )
    candidate = folder / filename
    if len(str(candidate.absolute())) <= _WINDOWS_MAX_PATH:
        return candidate

    folder_budget = (
        _WINDOWS_MAX_PATH - len(str(Path(output_root).absolute())) - len(filename) - 2
    )
    folder_name = _shorten_component(folder.name, folder_budget)
    candidate = Path(output_root) / folder_name / filename
    if len(str(candidate.absolute())) > _WINDOWS_MAX_PATH:
        raise ValueError("Output root is too long for a Windows-safe path")
    return candidate


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
