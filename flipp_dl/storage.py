"""Filename sanitization and output path helpers."""

from __future__ import annotations

import errno
import os
import shutil
import string
import tempfile
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


def move_file(source: Path, target: Path) -> None:
    """Move one file, falling back to verified copy on EXDEV only."""
    try:
        source.replace(target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    temporary_path: Path | None = None
    try:
        with (
            tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary,
            source.open("rb") as source_file,
        ):
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(source_file, temporary, length=1024 * 1024)
            # Force the copy to disk before it is renamed into place. A
            # restart treats any file at the target as already moved, so
            # a target that only exists in the page cache when the
            # machine dies would be accepted as complete while holding
            # nothing.
            temporary.flush()
            os.fsync(temporary.fileno())
        if temporary_path.stat().st_size != source.stat().st_size:
            raise OSError("Copied file size does not match the source")
        if target.exists():
            raise FileExistsError(target)
        os.replace(temporary_path, target)
        temporary_path = None
        # The rename itself needs the directory entry on disk too, or the
        # crash window just moves from the file to its name.
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        source.unlink()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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


def _is_just_the_date(issue_name: str, issue_date: str) -> bool:
    """Whether *issue_name* carries nothing but *issue_date* over again.

    Editions discovered through PageSuite (TASK-1439) have no issue
    number - PageSuite names them after their date, as ``DD/MM/YYYY``.
    Pasting that after the ISO date gives filenames like
    ``91an - 2020-02-20 - 20-02-2020.pdf``: the same date twice, in two
    notations. Compared digit by digit so both notations match, and so a
    name that merely contains a date ("Nr 3 2020, 20/02") is left alone.
    """
    name_digits = "".join(c for c in issue_name if c.isdigit())
    date_digits = "".join(c for c in issue_date if c.isdigit())
    if not name_digits or not date_digits:
        return False
    if any(c.isalpha() for c in issue_name):
        return False
    return sorted(name_digits) == sorted(date_digits)


def issue_filename(
    publication: Publication, issue: Issue, *, disambiguate: bool = False
) -> str:
    """Filename for one issue, optionally made unique.

    Publication, date and issue name are not unique on their own: Flipp
    publishes distinct issues that share all three (TASK-1349). Passing
    *disambiguate* appends part of the issue code so the second issue
    gets a file of its own instead of silently reusing the first one's.

    An issue whose name is only its own date, or has no name at all, is
    written without the name part - see :func:`_is_just_the_date`. That
    avoids both ``... - 2020-02-20 - 20-02-2020.pdf`` and a filename
    ending in a dangling separator.
    """
    if not (issue.issue_name or "").strip() or _is_just_the_date(
        issue.issue_name or "", issue.issue_date or ""
    ):
        stem = safe_name(f"{publication.name} - {issue.issue_date}")
    else:
        stem = safe_name(
            f"{publication.name} - {issue.issue_date} - {issue.issue_name}"
        )
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
