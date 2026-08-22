"""Shared logic for the catalogue and code-backup files.

The CLI (``--import-catalog``, ``--export-codes``, ``--import-backup``) and
the web UI both work on the same JSON shapes, so parsing, validation and the
database writes live here. The callers only add their own presentation:
``print`` in :mod:`flipp_dl.cli`, an HTMX partial in
:mod:`flipp_dl.web.codes_routes`.

The payload keys are part of the file format and must not be renamed:
publications carry ``customPublicationCode`` (the PageSuite pubid) and
``publicationCode`` (the human-readable SE-UVH identity), while an issue's
``publication_code`` points at its parent's *custom* code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from .db.models import DbIssue, DbPublication
from .models import Issue, Publication


class CodeFileError(ValueError):
    """A catalogue or backup file that could not be used.

    ``reason`` is a stable key the callers map to their own wording:
    ``unreadable`` (not JSON / not decodable), ``not_a_list`` or
    ``not_an_object`` (valid JSON of the wrong shape).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def parse_catalog(data: bytes | str) -> list:
    """Parse a catalogue file: a JSON list of publication entries."""
    parsed = _load_json(data)
    if not isinstance(parsed, list):
        raise CodeFileError("not_a_list", "expected a JSON list of publications")
    return parsed


def parse_backup(data: bytes | str) -> dict:
    """Parse a code backup: a JSON object with publications and issues."""
    parsed = _load_json(data)
    if not isinstance(parsed, dict):
        raise CodeFileError("not_an_object", "expected a JSON object")
    return parsed


def _load_json(data: bytes | str):
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CodeFileError("unreadable", str(exc)) from exc


@dataclass
class CatalogImportResult:
    """Counts from one catalogue import, per file half."""

    total: int = 0
    added: int = 0
    skipped: int = 0
    unlisted_total: int = 0
    unlisted_added: int = 0
    unlisted_skipped: int = 0
    codes_set: int = 0

    @property
    def known(self) -> int:
        """Publications in the catalogue that were already in the database."""
        return self.total - self.added - self.skipped

    @property
    def unlisted_known(self) -> int:
        return self.unlisted_total - self.unlisted_added - self.unlisted_skipped


@dataclass
class RestoreResult:
    """Counts from restoring a code backup."""

    publications: int = 0
    issues: int = 0
    missing_publications: list[str] = field(default_factory=list)


def import_publications(repo, entries: list, *, delisted: bool) -> tuple[int, int]:
    """Upsert publications from *entries*; return ``(added, skipped)``.

    Each entry needs ``customPublicationCode`` (== our ``custom_code``) and
    ``name``. When *delisted* is true the publications are marked delisted -
    used for the unlisted companion so they don't look like catalogue members.
    """
    added = skipped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        code = entry.get("customPublicationCode")
        name = entry.get("name")
        if not code or not name:
            skipped += 1
            continue
        existed = repo.get_publication(code) is not None
        db_pub = repo.upsert_publication(Publication(custom_code=code, name=name))
        if delisted:
            db_pub.delisted_at = datetime.now(UTC)
        if not existed:
            added += 1
    return added, skipped


def import_catalog(
    repo, entries: list, unlisted: list | None = None
) -> CatalogImportResult:
    """Add missing publications from a catalogue, plus the unlisted companion.

    Existing publications are left as they are; only missing ones are
    inserted. ``publicationCode`` is backfilled wherever the catalogue
    carries it, since that is what disambiguates same-named publications.
    """
    unlisted = unlisted or []
    code_by_custom_code = {
        e["customPublicationCode"]: e["publicationCode"]
        for e in entries
        if isinstance(e, dict)
        and e.get("customPublicationCode")
        and e.get("publicationCode")
    }
    added, skipped = import_publications(repo, entries, delisted=False)
    u_added, u_skipped = import_publications(repo, unlisted, delisted=True)
    coded = repo.backfill_publication_codes(code_by_custom_code)
    return CatalogImportResult(
        total=len(entries),
        added=added,
        skipped=skipped,
        unlisted_total=len(unlisted),
        unlisted_added=u_added,
        unlisted_skipped=u_skipped,
        codes_set=coded,
    )


def build_backup_payload(session) -> dict:
    """Collect every pubid and eid in the database into a backup payload.

    The codes are the irreplaceable part - a lost database can rebuild its
    downloads from them, so the backup carries names and dates too and needs
    no network calls to be restored.
    """
    pubs = list(session.scalars(select(DbPublication)))
    code_by_id = {p.id: p.custom_code for p in pubs}
    publications = [
        {
            "customPublicationCode": p.custom_code,
            "publicationCode": p.publication_code,
            "name": p.name,
            "delisted": p.delisted_at is not None,
        }
        for p in pubs
    ]
    issues = [
        {
            "issue_code": i.custom_code,
            "publication_code": code_by_id.get(i.publication_id),
            "issue_name": i.issue_name,
            "issue_date": i.issue_date,
        }
        for i in session.scalars(select(DbIssue))
    ]
    return {"publications": publications, "issues": issues}


def dump_backup_payload(payload: dict) -> str:
    """Serialise a backup payload the way both callers write it out."""
    return json.dumps(payload, ensure_ascii=False, indent=1)


def restore_backup(repo, payload: dict) -> RestoreResult:
    """Restore publications and issue codes from a backup payload."""
    publications = payload.get("publications") or []
    issues = payload.get("issues") or []
    if not isinstance(publications, list):
        publications = []
    if not isinstance(issues, list):
        issues = []

    code_map: dict[str, str] = {}
    restored_pubs = 0
    for entry in publications:
        if not isinstance(entry, dict):
            continue
        code = entry.get("customPublicationCode")
        name = entry.get("name")
        if not code or not name:
            continue
        db_pub = repo.upsert_publication(Publication(custom_code=code, name=name))
        if entry.get("delisted"):
            db_pub.delisted_at = datetime.now(UTC)
        if entry.get("publicationCode"):
            code_map[code] = entry["publicationCode"]
        restored_pubs += 1
    repo.backfill_publication_codes(code_map)

    by_pub: dict[str, list[Issue]] = {}
    for entry in issues:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("issue_code")
        pubid = entry.get("publication_code")
        if not eid or not pubid:
            continue
        by_pub.setdefault(pubid, []).append(
            Issue(
                custom_code=eid,
                issue_name=entry.get("issue_name", "") or "",
                issue_date=entry.get("issue_date", "") or "",
            )
        )
    result = RestoreResult(publications=restored_pubs)
    for pubid, pub_issues in by_pub.items():
        db_pub = repo.get_publication(pubid)
        if db_pub is None:
            result.missing_publications.append(pubid)
            continue
        result.issues += len(repo.discover_editions(db_pub.id, pub_issues))
    return result
