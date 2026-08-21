"""Data-access layer for flipp-dl.

:class:`DownloadRepository` is the single point of contact between the
application logic (downloader, scheduler, web UI) and the database.  It
accepts a :class:`sqlalchemy.orm.Session` so callers control transaction
boundaries.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import storage
from ..models import Issue as DomainIssue
from ..models import Publication as DomainPublication
from .models import (
    DbIssue,
    DbJob,
    DbPublication,
    DbPublicationCategory,
    DbSetting,
    IssueStatus,
    JobStatus,
)

logger = logging.getLogger(__name__)

# Content-Type -> filename extension for cached cover images. Anything not
# in this map is treated as "not actually an image" and rejected.
_COVER_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_DEFAULT_COVER_TIMEOUT = 15


def default_cover_cache_root() -> Path:
    """Where locally cached publication/issue covers live on disk.

    ``FLIPP_COVER_CACHE`` wins if set; otherwise the cache lives next to
    the database file (``FLIPP_DB``, default ``flipp.db``). Deliberately
    never under ``output_root``: the Library view and the disk importer
    (``import_existing_files``) scan that directory for PDFs and would
    otherwise mistake a cached cover for a download - the same trap the
    preview cache (``downloader.default_preview_root``) already avoids.
    """
    env = os.environ.get("FLIPP_COVER_CACHE")
    if env:
        return Path(env)
    db_path = Path(os.environ.get("FLIPP_DB", "flipp.db"))
    return db_path.resolve().parent / "flipp-dl-covers"


def fetch_and_cache_cover(
    url: str,
    cache_root: Path,
    filename_stem: str,
    *,
    http_session: requests.Session | None = None,
    timeout: int = _DEFAULT_COVER_TIMEOUT,
) -> str | None:
    """Download *url* and save it under *cache_root*, returning the filename.

    Returns ``None`` (and logs a warning) on any network error, non-200
    response, or a Content-Type that doesn't look like an image - callers
    treat that as "leave the cache as it was" rather than a hard failure,
    since a poll tick covers many publications/issues and one bad cover
    shouldn't abort the rest.

    Writes to a temp file first and renames into place so a request that
    arrives mid-download never sees a half-written file.
    """
    session = http_session or requests
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Cover fetch failed for %s: %s", url, exc)
        return None

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    ext = _COVER_CONTENT_TYPES.get(content_type)
    if ext is None:
        logger.warning(
            "Cover fetch for %s returned unexpected Content-Type %r - skipping",
            url,
            content_type,
        )
        return None

    cache_root.mkdir(parents=True, exist_ok=True)
    filename = f"{filename_stem}{ext}"
    dest = cache_root / filename
    tmp_dest = cache_root / f".{filename}.{uuid.uuid4().hex}.tmp"
    try:
        tmp_dest.write_bytes(resp.content)
        tmp_dest.replace(dest)
    except OSError as exc:
        logger.warning("Failed to write cached cover %s: %s", dest, exc)
        tmp_dest.unlink(missing_ok=True)
        return None
    return filename


def find_cached_cover(cache_root: Path, filename_stem: str) -> Path | None:
    """Return an already-cached cover file for *filename_stem*, if any.

    Tries every extension :func:`fetch_and_cache_cover` can produce,
    since the content type (and therefore the extension) isn't known
    ahead of time. Used by the lightbox cover routes (TASK-1396) to
    avoid re-fetching a large cover that's already on disk - no DB
    column tracks these, the filesystem itself is the cache index.
    """
    for ext in _COVER_CONTENT_TYPES.values():
        candidate = cache_root / f"{filename_stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Default warning threshold for the "Queue missing issues" backfill button
# (TASK-1361): above this many estimated bytes, the confirm dialog must
# spell out the size explicitly rather than rely on today's plain
# count/size confirm. 5 GiB is roughly a hundred issues at the production
# instance's ~49 MB average - comfortably above a routine catch-up, well
# below the ~800 GB a full 16568-issue backfill would cost.
_DEFAULT_QUEUE_WARN_THRESHOLD_BYTES = 5 * 1024**3
_QUEUE_WARN_THRESHOLD_SETTING = "queue_warn_threshold_bytes"
_QUEUE_WARN_THRESHOLD_ENV = "FLIPP_QUEUE_WARN_THRESHOLD_BYTES"

# Automatic retry backoff for failed download jobs (TASK-1363). One entry
# per attempt, in minutes; the length of the tuple is the retry ceiling.
# Growing (5, 15, 45 min) so a real network blip clears on the first or
# second attempt while a longer-lived outage doesn't hammer Flipp every
# few minutes. Three attempts, not more: past that a failure has stopped
# looking transient and should sit as an explicit ``error`` again rather
# than keep retrying silently forever.
RETRY_DELAYS_MINUTES: tuple[int, ...] = (5, 15, 45)
MAX_AUTO_RETRIES = len(RETRY_DELAYS_MINUTES)


def _parse_positive_int(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _issue_identity(issue: DbIssue) -> dict:
    """A compact, JSON-friendly identity for *issue* used in reports."""
    return {
        "issue_id": issue.id,
        "publication": issue.publication.name if issue.publication else None,
        "issue_name": issue.issue_name,
    }


@dataclass
class ImportReport:
    """Result of reconciling on-disk files against the issues table.

    Produced by :meth:`DownloadRepository.import_existing_files`
    (TASK-1283). Covers both directions of drift a manual sweep of the
    production instance found on 2026-08-18: an issue that sat on disk
    while the DB still called it ``queued``, and two issues claiming the
    same file - one of which was therefore never really downloaded.
    """

    backfilled: list[dict] = field(default_factory=list)
    sized: list[dict] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    misplaced_files: list[dict] = field(default_factory=list)
    missing_files: list[dict] = field(default_factory=list)
    shared_files: list[dict] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(
            self.orphan_files
            or self.misplaced_files
            or self.missing_files
            or self.shared_files
        )


@dataclass
class QueueSizeEstimate:
    """Estimated download size for a publication's not-yet-downloaded issues.

    Produced by :meth:`DownloadRepository.estimate_missing_download_size`
    (TASK-1362), which does no rendering and no filesystem access - only
    ``issues.file_size`` values already captured on previous downloads -
    so the same call answers both the bulk-queue confirm dialog and the
    backfill size-threshold warning (TASK-1361).
    """

    issue_count: int
    estimated_bytes: int | None
    basis: str  # "publication" | "global" | "none"


class PublicationFolderError(ValueError):
    """The selected publication folder name cannot be used."""


class PublicationFolderConflict(PublicationFolderError):
    """The selected folder is already owned by another publication."""


class PublicationFolderMoveError(PublicationFolderError):
    """Existing downloaded files could not be moved safely."""


class PublicationDestinationError(ValueError):
    """A destination change is not allowed for this publication."""


class DownloadRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Publications
    # ------------------------------------------------------------------

    def upsert_publication(self, pub: DomainPublication) -> DbPublication:
        """Insert or update a publication and its categories.

        Returns the persisted :class:`DbPublication` (not yet committed).
        """
        db_pub = self.session.scalar(
            select(DbPublication).where(DbPublication.custom_code == pub.custom_code)
        )
        if db_pub is None:
            db_pub = DbPublication(
                custom_code=pub.custom_code,
                name=pub.name,
                cover_url=pub.cover_url,
                description=pub.description,
                next_issue_date=pub.next_issue_date,
            )
            self.session.add(db_pub)
            self.session.flush()  # get id
        else:
            db_pub.name = pub.name
            if pub.cover_url:
                db_pub.cover_url = pub.cover_url
            # Description / next_issue_date are allowed to be cleared
            # on a later poll so always assign.
            db_pub.description = pub.description
            db_pub.next_issue_date = pub.next_issue_date

        # Sync categories (replace all)
        for cat_row in db_pub.categories:
            self.session.delete(cat_row)
        self.session.flush()
        for cat in pub.categories:
            self.session.add(
                DbPublicationCategory(
                    publication_id=db_pub.id,
                    category_id=cat.id,
                    category_name=cat.name,
                )
            )
        return db_pub

    def get_publication(self, custom_code: str) -> DbPublication | None:
        return self.session.scalar(
            select(DbPublication)
            .options(
                selectinload(DbPublication.categories),
                selectinload(DbPublication.issues),
            )
            .where(DbPublication.custom_code == custom_code)
        )

    def backfill_publication_codes(self, code_by_custom_code: dict[str, str]) -> int:
        """Set publication_code where custom_code is in the mapping.

        Returns count updated. Only sets it where currently different;
        never clears it.
        """
        updated = 0
        for publication in self.session.scalars(select(DbPublication)):
            publication_code = code_by_custom_code.get(publication.custom_code)
            if (
                publication_code is not None
                and publication.publication_code != publication_code
            ):
                publication.publication_code = publication_code
                updated += 1
        return updated

    def list_publications(self, watched_only: bool = False) -> list[DbPublication]:
        """Return publications with issue counts, without loading issues.

        The list view only ever renders ``num_issues``/``num_downloaded``
        per row - selectinload-ing the whole ``issues`` relationship used
        to drag in every issue in the database (17660 rows on the
        production instance) just to compute two integers (TASK-1338).
        The counts are aggregated in the DB instead and handed to each
        row via the ``num_issues``/``num_downloaded`` setters. The same
        query also aggregates the on-disk size (TASK-1379): summing
        ``file_size`` and counting how many downloaded issues are missing
        it are both cheap additions to a query that's already grouping by
        publication, so there's no separate round-trip and no per-row
        filesystem stat().
        """
        q = select(DbPublication).options(selectinload(DbPublication.categories))
        if watched_only:
            q = q.where(DbPublication.watched == True)  # noqa: E712
        pubs = list(self.session.scalars(q))
        counts = self._issue_counts_by_publication()
        for pub in pubs:
            total, done, size_bytes, size_unknown = counts.get(pub.id, (0, 0, 0, 0))
            pub.num_issues = total
            pub.num_downloaded = done
            pub.size_bytes = size_bytes
            pub.size_unknown_count = size_unknown
        return pubs

    def _issue_counts_by_publication(
        self,
    ) -> dict[int, tuple[int, int, int, int]]:
        """Return ``{publication_id: (total, done, size_bytes, size_unknown)}``.

        One grouped query over the issues table instead of one row per
        issue - see :meth:`list_publications`. ``size_bytes`` sums only the
        downloaded issues with a known ``file_size``; ``size_unknown`` counts
        downloaded issues where it's ``NULL`` (pre-TASK-1362 data) so the
        caller can tell an undercount from a genuine zero.
        """
        is_done = DbIssue.status == IssueStatus.DONE
        rows = self.session.execute(
            select(
                DbIssue.publication_id,
                func.count(DbIssue.id),
                func.sum(case((is_done, 1), else_=0)),
                func.sum(case((is_done, func.coalesce(DbIssue.file_size, 0)), else_=0)),
                func.sum(case((is_done & DbIssue.file_size.is_(None), 1), else_=0)),
            ).group_by(DbIssue.publication_id)
        ).all()
        return {
            publication_id: (
                int(total),
                int(done or 0),
                int(size_bytes or 0),
                int(size_unknown or 0),
            )
            for publication_id, total, done, size_bytes, size_unknown in rows
        }

    def set_watched(self, custom_code: str, enabled: bool) -> bool:
        """Toggle the watch flag. Returns False if publication not found.

        Enabling stamps ``watch_started_at`` to now (TASK-1361) - poll's
        catch-up pass uses it as the cutoff for what counts as "the back
        catalogue" versus "discovered while watching". Re-enabling after
        an unwatch resets it too: whatever accumulated in the meantime is
        backlog again, not something a poll should silently pick up.
        """
        db_pub = self.get_publication(custom_code)
        if db_pub is None:
            return False
        db_pub.watched = enabled
        if enabled:
            db_pub.watch_started_at = _now()
        return True

    @staticmethod
    def _publication_folder_component(publication: DbPublication) -> str:
        return storage.publication_folder(Path(), publication).name

    def publication_folder_conflict(self, custom_code: str) -> DbPublication | None:
        """Return the other publication claiming the same folder, if any."""
        publication = self.get_publication(custom_code)
        if publication is None:
            return None
        component = self._publication_folder_component(publication).casefold()
        for other in self.session.scalars(
            select(DbPublication).where(DbPublication.id != publication.id)
        ):
            if self._publication_folder_component(other).casefold() == component:
                return other
        return None

    def _disable_watched_folder_collisions(self) -> None:
        """Pause watched publications whose effective folders now collide.

        Flipp can rename a publication after watching was enabled. Pausing
        every affected publication makes the next Watch click ask for an
        explicit folder name instead of letting a later poll download into
        an ambiguous directory.
        """
        by_component: dict[str, list[DbPublication]] = {}
        for publication in self.session.scalars(select(DbPublication)):
            component = self._publication_folder_component(publication).casefold()
            by_component.setdefault(component, []).append(publication)
        for publications in by_component.values():
            if len(publications) < 2:
                continue
            for publication in publications:
                if publication.watched:
                    publication.watched = False
                    logger.warning(
                        "Paused watching %s because its publication folder collides",
                        publication.custom_code,
                    )

    def set_publication_folder_name(
        self, custom_code: str, folder_name: str, output_root: Path
    ) -> bool:
        """Set a user-owned folder name and move this publication's files.

        Returns False if the publication does not exist. Files tracked by
        issues are moved one by one so an old folder shared by two
        publications is never moved wholesale.
        """
        publication = self.get_publication(custom_code)
        if publication is None:
            return False

        selected = folder_name.strip()
        if not selected:
            selected_value: str | None = None
        elif storage.safe_name(selected) != selected:
            raise PublicationFolderError(
                "Use only characters that are valid in a folder name."
            )
        else:
            selected_value = selected

        old_folder = storage.publication_folder(output_root, publication).resolve()
        previous = publication.folder_name
        publication.folder_name = selected_value
        new_folder = storage.publication_folder(output_root, publication).resolve()

        conflict = self.publication_folder_conflict(custom_code)
        if conflict is not None:
            publication.folder_name = previous
            raise PublicationFolderConflict(
                f'Folder name is already used by "{conflict.name}".'
            )

        if old_folder == new_folder:
            return True

        moves: list[tuple[Path, Path, DbIssue, str]] = []
        for issue in publication.issues:
            if not issue.file_path:
                continue
            source = storage.resolve_safe_path(output_root, issue.file_path)
            if source is None:
                continue
            try:
                relative = source.relative_to(old_folder)
            except ValueError:
                continue
            target = new_folder / relative
            if target.exists():
                publication.folder_name = previous
                raise PublicationFolderMoveError(
                    f'Cannot move files because "{target.name}" already exists.'
                )
            moves.append((source, target, issue, issue.file_path))

        completed: list[tuple[Path, Path, DbIssue, str]] = []
        try:
            for source, target, issue, old_file_path in moves:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
                if Path(old_file_path).is_absolute():
                    issue.file_path = str(target.resolve())
                else:
                    issue.file_path = str(
                        target.resolve().relative_to(Path(output_root).resolve())
                    )
                completed.append((source, target, issue, old_file_path))
        except OSError as exc:
            for source, target, issue, old_file_path in reversed(completed):
                source.parent.mkdir(parents=True, exist_ok=True)
                target.replace(source)
                issue.file_path = old_file_path
            publication.folder_name = previous
            raise PublicationFolderMoveError(
                "The downloaded files could not be moved."
            ) from exc
        return True

    def set_publication_destination(self, custom_code: str, destination: str) -> bool:
        """Set the output destination unless downloaded issues already exist."""
        publication = self.get_publication(custom_code)
        if publication is None:
            return False

        selected = destination.strip()
        if selected not in {"", "primary", "secondary"}:
            raise ValueError("Unknown publication destination")
        selected_value = "secondary" if selected == "secondary" else None
        if publication.destination == selected_value:
            return True
        if any(issue.status == IssueStatus.DONE for issue in publication.issues):
            raise PublicationDestinationError(
                "Downloaded issues prevent changing the destination."
            )
        publication.destination = selected_value
        return True

    def mark_polled(self, custom_code: str) -> None:
        db_pub = self.get_publication(custom_code)
        if db_pub:
            db_pub.last_polled_at = _now()

    def set_publication_poll_interval(
        self, custom_code: str, minutes: int | None
    ) -> bool:
        """Set (or clear) a publication's own poll interval override.

        ``minutes=None`` reverts to the global default - the publication
        is queued/backfilled on every poll tick again, same as before it
        ever had an override. Also resets ``next_poll_due_at`` so a
        shortened interval takes effect on the very next tick instead of
        waiting out whatever the previous interval had scheduled.
        Returns False if the publication doesn't exist.
        """
        db_pub = self.get_publication(custom_code)
        if db_pub is None:
            return False
        db_pub.poll_interval_minutes = minutes
        db_pub.next_poll_due_at = None
        return True

    def publications_due_for_poll(self, publication_ids: set[int]) -> set[int]:
        """Return the subset of *publication_ids* due to be polled now.

        A publication without an interval override is always due - that
        keeps today's behaviour of being queued/backfilled on every
        global poll tick. One with an override is due only once its own
        interval has elapsed since :meth:`mark_publication_poll_done` was
        last called for it.
        """
        if not publication_ids:
            return set()
        now = _now()
        due: set[int] = set()
        pubs = self.session.scalars(
            select(DbPublication).where(DbPublication.id.in_(publication_ids))
        )
        for pub in pubs:
            # Same truthiness test as mark_publication_poll_done: a stored
            # 0 counts as "no override" in both places, so the two can't
            # disagree about whether a publication has one.
            if not pub.poll_interval_minutes:
                due.add(pub.id)
            elif pub.next_poll_due_at is None or pub.next_poll_due_at <= now:
                due.add(pub.id)
        return due

    def mark_publication_poll_done(self, publication_id: int) -> None:
        """Advance ``next_poll_due_at`` after processing a due publication.

        No-op for a publication without its own override - it has
        nothing to advance and stays due on every tick.
        """
        db_pub = self.get_publication_by_id(publication_id)
        if db_pub is not None and db_pub.poll_interval_minutes:
            db_pub.next_poll_due_at = _now() + timedelta(
                minutes=db_pub.poll_interval_minutes
            )

    def publications_needing_cover_refresh(self) -> list[DbPublication]:
        """Publications whose ``cover_url`` hasn't been cached yet (TASK-1345).

        A publication needs a (re)fetch when it has a ``cover_url`` that
        either was never cached, or differs from what was cached last
        time - i.e. Flipp published a new cover. Cheap to call every poll
        tick since it's a plain column comparison, no join.
        """
        return list(
            self.session.scalars(
                select(DbPublication).where(
                    DbPublication.cover_url.is_not(None),
                    or_(
                        DbPublication.cover_cache_source_url.is_(None),
                        DbPublication.cover_url != DbPublication.cover_cache_source_url,
                    ),
                )
            )
        )

    def set_publication_cover_cache(
        self, publication_id: int, cache_filename: str, source_url: str
    ) -> None:
        db_pub = self.get_publication_by_id(publication_id)
        if db_pub is not None:
            db_pub.cover_cache_path = cache_filename
            db_pub.cover_cache_source_url = source_url

    def get_publication_by_id(self, publication_id: int) -> DbPublication | None:
        return self.session.get(DbPublication, publication_id)

    def set_komga_series_id(self, custom_code: str, series_id: int) -> bool:
        """Cache the Komga series a publication maps to (TASK-1327).

        Called once, the first time the folder-name lookup against
        Komga's search endpoint succeeds - the mapping never changes
        afterwards, so this is the only writer of the column. Returns
        False if the publication doesn't exist.
        """
        db_pub = self.get_publication(custom_code)
        if db_pub is None:
            return False
        db_pub.komga_series_id = series_id
        return True

    def get_unmapped_publications(self) -> list[DbPublication]:
        """Publications with no Komga series mapped yet.

        Used by the detail page (and any future bulk-remap tooling) to
        show "Komga: okänd - söker nästa gång" without a per-row query.
        """
        return list(
            self.session.scalars(
                select(DbPublication).where(DbPublication.komga_series_id.is_(None))
            )
        )

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def upsert_issue(
        self, issue: DomainIssue, publication_id: int
    ) -> tuple[DbIssue, bool]:
        """Insert a new issue or return the existing one.

        Returns ``(db_issue, created)`` where *created* is True for new rows.
        """
        db_issue = self.session.scalar(
            select(DbIssue).where(
                DbIssue.publication_id == publication_id,
                DbIssue.custom_code == issue.custom_code,
            )
        )
        if db_issue is not None:
            return db_issue, False

        db_issue = DbIssue(
            publication_id=publication_id,
            custom_code=issue.custom_code,
            issue_name=issue.issue_name,
            issue_date=issue.issue_date,
            status=IssueStatus.NEW,
        )
        self.session.add(db_issue)
        self.session.flush()
        return db_issue, True

    def set_issue_cover_cache(self, issue_id: int, cache_filename: str) -> None:
        db_issue = self.get_issue(issue_id)
        if db_issue is not None:
            db_issue.cover_cache_path = cache_filename

    def issues_needing_cover_backfill(self, limit: int) -> list[DbIssue]:
        """Issues discovered before the cover cache existed (TASK-1374).

        Issue covers are normally fetched once, at discovery
        (:meth:`sync_publications`/``scheduler._cache_covers``). Every
        issue discovered before that fetch existed (TASK-1345) never got
        one and never will via that path alone, so this backfills them
        gradually.

        Downloaded issues come first, most recently downloaded before
        the rest: those are the ones on the dashboard's recent
        downloads, in the library, and in any reader picking the feed
        up. Ordering purely by discovery id left a freshly downloaded
        back-catalogue issue at the very end of a 17660-row queue, which
        is exactly the gap that showed as blank covers on the dashboard.
        Everything else follows newest-discovered-first. *limit* bounds
        how many rows come back; the caller calls this once per poll
        tick so the backlog fills in gradually rather than in one burst
        of external requests.
        """
        return list(
            self.session.scalars(
                select(DbIssue)
                .where(DbIssue.cover_cache_path.is_(None))
                .order_by(
                    (DbIssue.status != IssueStatus.DONE),
                    DbIssue.downloaded_at.desc().nullslast(),
                    DbIssue.id.desc(),
                )
                .limit(limit)
            )
        )

    def set_komga_book_id(self, issue_id: int, book_id: int) -> bool:
        """Cache the Komga book an issue maps to (TASK-1328).

        Written once, the moment the nivå-2 metadata push
        (:mod:`flipp_dl.scheduler`) matches a book by filename stem - the
        daily read-status sync reads this instead of redoing that lookup.
        Returns False if the issue doesn't exist.
        """
        db_issue = self.get_issue(issue_id)
        if db_issue is None:
            return False
        db_issue.komga_book_id = book_id
        return True

    def get_issues_with_komga_book_id(self) -> list[DbIssue]:
        """Issues mapped to a Komga book - candidates for the daily
        read-status sync (TASK-1328)."""
        return list(
            self.session.scalars(
                select(DbIssue).where(DbIssue.komga_book_id.is_not(None))
            )
        )

    def set_issue_read_status(
        self, issue_id: int, *, read: bool, page: int, synced_at: datetime
    ) -> bool:
        """Store the read status the daily sync fetched from Komga.

        A Komga failure never calls this - the caller skips the issue
        and keeps whatever was cached last, so a flaky Komga instance
        never regresses a known read status back to unknown.
        """
        db_issue = self.get_issue(issue_id)
        if db_issue is None:
            return False
        db_issue.komga_read = read
        db_issue.komga_read_page = page
        db_issue.komga_read_synced_at = synced_at
        return True

    def get_issue(self, issue_id: int) -> DbIssue | None:
        return self.session.get(DbIssue, issue_id)

    def get_issue_by_code(
        self, custom_code: str, publication_id: int
    ) -> DbIssue | None:
        return self.session.scalar(
            select(DbIssue).where(
                DbIssue.custom_code == custom_code,
                DbIssue.publication_id == publication_id,
            )
        )

    def get_issue_by_file_path(self, file_path: str) -> DbIssue | None:
        """Return the issue that owns *file_path*, if any."""
        return self.session.scalar(
            select(DbIssue).where(DbIssue.file_path == str(file_path))
        )

    def list_issues_sharing_files(self) -> list[list[DbIssue]]:
        """Group issues that claim the same file on disk.

        Two issues pointing at one file means one of them was never
        actually downloaded - its content is nowhere (TASK-1349).
        """
        by_path: dict[str, list[DbIssue]] = {}
        rows = self.session.scalars(
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.file_path.is_not(None))
        )
        for issue in rows:
            by_path.setdefault(issue.file_path, []).append(issue)
        return [group for group in by_path.values() if len(group) > 1]

    def import_existing_files(
        self, output_root: Path, extra_roots: list[Path] | None = None
    ) -> ImportReport:
        """Reconcile issue status with what is actually on disk.

        Read-only on the filesystem - only the DB is written. A file on
        disk whose expected name matches an issue that isn't ``done``
        yet backfills that issue (status, ``file_path``,
        ``downloaded_at``) without re-downloading anything. A file only
        ever backfills an issue if it resolves inside *output_root* -
        the same containment check the web layer uses
        (:func:`storage.resolve_safe_path`) - so a crafted or stale path
        can never mark an issue done from outside the managed tree.

        Everything the scan can't cleanly explain is reported rather
        than silently fixed: files with no matching issue (orphans),
        files whose names match issues from another publication,
        ``done`` issues whose file has disappeared, issues that already
        share one file with another (see :meth:`list_issues_sharing_files`,
        TASK-1349), and a not-yet-done issue whose expected filename is
        already claimed by a *different* ``done`` issue - backfilling
        that one blindly would recreate exactly the same bug instead of
        catching it.
        """
        candidates = [Path(output_root), *(extra_roots or [])]
        roots: list[Path] = []
        for candidate in candidates:
            try:
                root = candidate.resolve()
            except OSError:
                root = candidate
            if root not in roots:
                roots.append(root)
        primary_root = roots[0]
        secondary_root = roots[1] if len(roots) > 1 else None

        issues = sorted(
            self.session.scalars(
                select(DbIssue).options(selectinload(DbIssue.publication))
            ),
            key=lambda i: i.id,
        )

        # Every path a real download could have produced for this issue:
        # the plain name and the disambiguated form two issues get when
        # they'd otherwise collide (TASK-1349). Lowest id wins a clash
        # on the plain form so the result is deterministic.
        publication_roots: dict[Path, set[int]] = {}
        for publication in self.session.scalars(select(DbPublication)):
            destination = storage.destination_root(
                primary_root, secondary_root, publication
            )
            publication_root = storage.publication_folder(
                destination, publication
            ).resolve()
            publication_roots.setdefault(publication_root, set()).add(publication.id)

        by_path: dict[Path, DbIssue] = {}
        by_filename: dict[str, list[tuple[DbIssue, Path]]] = {}
        for issue in issues:
            pub = issue.publication
            if pub is None:
                continue
            destination = storage.destination_root(primary_root, secondary_root, pub)
            publication_root = storage.publication_folder(destination, pub).resolve()
            for disambiguate in (False, True):
                path = storage.issue_path(
                    destination, pub, issue, disambiguate=disambiguate
                )
                by_path.setdefault(path, issue)
                by_filename.setdefault(path.name, []).append((issue, publication_root))

        report = ImportReport()

        for root in roots:
            if not root.is_dir():
                continue
            for pdf in sorted(root.rglob("*.pdf")):
                if not pdf.is_file():
                    continue
                try:
                    resolved = pdf.resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue  # escapes this managed root via a symlink - ignore
                matching_by_name = by_filename.get(resolved.name, [])
                containing_publication_ids = publication_roots.get(
                    resolved.parent, set()
                )
                if len(containing_publication_ids) > 1 and matching_by_name:
                    report.misplaced_files.append(
                        {
                            "file_path": str(resolved.relative_to(root)),
                            "matching_issues": [
                                _issue_identity(candidate)
                                for candidate, _ in matching_by_name
                            ],
                        }
                    )
                    continue

                issue = by_path.get(resolved)
                if issue is None:
                    matching_elsewhere = []
                    for candidate, publication_root in matching_by_name:
                        try:
                            resolved.relative_to(publication_root)
                        except ValueError:
                            matching_elsewhere.append(candidate)
                    if matching_elsewhere:
                        report.misplaced_files.append(
                            {
                                "file_path": str(resolved.relative_to(root)),
                                "matching_issues": [
                                    _issue_identity(candidate)
                                    for candidate in matching_elsewhere
                                ],
                            }
                        )
                        continue
                    report.orphan_files.append(str(resolved.relative_to(root)))
                    continue
                if issue.status == IssueStatus.DONE:
                    # Already downloaded, but issues downloaded before
                    # file_size existed (TASK-1362) have no size recorded,
                    # which leaves the publication list showing "unknown".
                    # Fill it in without touching status or downloaded_at.
                    if issue.file_size is None:
                        try:
                            issue.file_size = resolved.stat().st_size
                        except OSError:
                            pass
                        else:
                            report.sized.append(
                                {
                                    **_issue_identity(issue),
                                    "file_size": issue.file_size,
                                }
                            )
                    continue

                # Another issue may already have this exact path recorded
                # as its file_path - e.g. it was the one actually
                # downloaded when two issues collided on the plain name
                # (TASK-1349). Backfilling *this* issue on top of it would
                # silently recreate that bug, so report it instead.
                owner = self.get_issue_by_file_path(str(resolved))
                if owner is not None and owner.id != issue.id:
                    report.shared_files.append(
                        {
                            "file_path": str(resolved),
                            "issues": [
                                {**_issue_identity(owner), "status": owner.status},
                                {**_issue_identity(issue), "status": issue.status},
                            ],
                        }
                    )
                    continue

                try:
                    size = resolved.stat().st_size
                except OSError:
                    size = None
                self.mark_issue_done(issue.id, str(resolved), file_size=size)
                report.backfilled.append(
                    {**_issue_identity(issue), "file_path": str(resolved)}
                )

        for issue in issues:
            if issue.status != IssueStatus.DONE or not issue.file_path:
                continue
            if not any(
                storage.resolve_safe_path(root, issue.file_path) is not None
                for root in roots
            ):
                report.missing_files.append(
                    {**_issue_identity(issue), "file_path": issue.file_path}
                )

        for group in self.list_issues_sharing_files():
            report.shared_files.append(
                {
                    "file_path": group[0].file_path,
                    "issues": [
                        {**_issue_identity(issue), "status": issue.status}
                        for issue in group
                    ],
                }
            )

        return report

    def list_issues(
        self,
        publication_id: int | None = None,
        status: str | None = None,
    ) -> list[DbIssue]:
        q = select(DbIssue).options(selectinload(DbIssue.publication))
        if publication_id is not None:
            q = q.where(DbIssue.publication_id == publication_id)
        if status is not None:
            q = q.where(DbIssue.status == status)
        return list(self.session.scalars(q))

    # Cap for the cross-publication search (TASK-1364). The production
    # instance has 17660+ issues - loading them all and filtering in
    # Python (or handing them to the browser to filter in JavaScript,
    # like the smaller /publications and /publications/<code> filters
    # do) is exactly the trap TASK-1338 removed from the publication
    # list. One bounded, indexed query instead.
    SEARCH_ISSUE_LIMIT = 200

    def search_issues(
        self,
        query: str | None = None,
        status: str | None = None,
        downloaded: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[DbIssue], bool]:
        """Search issues by name/date/publication across all publications.

        ``query`` matches (case-insensitively) against the issue name,
        the issue date string, and the owning publication's name.
        ``status`` restricts to one :class:`IssueStatus` value.
        ``downloaded`` is ``"yes"`` (only ``done``), ``"no"`` (anything
        but ``done``), or ``None``/anything else for no restriction.

        Fetches ``limit + 1`` rows and trims the extra one so the caller
        can tell "there may be more, narrow your search" apart from "this
        is everything" without a separate ``COUNT(*)`` query. Returns
        ``(issues, has_more)``.
        """
        if limit is None:
            # Resolved via the class, not a bound default argument, so
            # tests (and any future caller) can override
            # ``DownloadRepository.SEARCH_ISSUE_LIMIT`` and have it take
            # effect - a mutable default bound at function-definition time
            # wouldn't pick that up.
            limit = type(self).SEARCH_ISSUE_LIMIT
        q = (
            select(DbIssue)
            .join(DbPublication, DbIssue.publication_id == DbPublication.id)
            .options(selectinload(DbIssue.publication))
        )
        text = (query or "").strip()
        if text:
            like = f"%{text}%"
            q = q.where(
                or_(
                    DbIssue.issue_name.ilike(like),
                    DbIssue.issue_date.ilike(like),
                    DbPublication.name.ilike(like),
                )
            )
        if status:
            q = q.where(DbIssue.status == status)
        if downloaded == "yes":
            q = q.where(DbIssue.status == IssueStatus.DONE)
        elif downloaded == "no":
            q = q.where(DbIssue.status != IssueStatus.DONE)
        q = q.order_by(DbIssue.issue_date.desc(), DbIssue.id.desc()).limit(limit + 1)
        rows = list(self.session.scalars(q))
        has_more = len(rows) > limit
        return rows[:limit], has_more

    def count_issues_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over the issues table.

        Counting in the DB instead of loading every row matters on a
        real instance - the dashboard polls this and there are tens of
        thousands of issues.
        """
        rows = self.session.execute(
            select(DbIssue.status, func.count(DbIssue.id)).group_by(DbIssue.status)
        ).all()
        counts = {
            IssueStatus.NEW.value: 0,
            IssueStatus.QUEUED.value: 0,
            IssueStatus.DOWNLOADING.value: 0,
            IssueStatus.DONE.value: 0,
            IssueStatus.ERROR.value: 0,
        }
        for status, count in rows:
            counts[status] = int(count)
        return counts

    def count_publications(self) -> tuple[int, int]:
        """Return ``(total, watched)`` publication counts."""
        total = int(self.session.scalar(select(func.count(DbPublication.id))) or 0)
        watched = int(
            self.session.scalar(
                select(func.count(DbPublication.id)).where(
                    DbPublication.watched == True  # noqa: E712
                )
            )
            or 0
        )
        return total, watched

    def list_recent_downloads(self, limit: int = 10) -> list[DbIssue]:
        """Return the most recently downloaded issues, newest first."""
        q = (
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.status == IssueStatus.DONE)
            .order_by(DbIssue.downloaded_at.desc(), DbIssue.id.desc())
            .limit(limit)
        )
        return list(self.session.scalars(q))

    def get_issues_by_ids(self, issue_ids: list[int]) -> dict[int, DbIssue]:
        """Return ``{issue_id: DbIssue}`` for the given ids, publication loaded.

        One query for the whole page - the jobs list would otherwise do a
        lookup per row just to name what each job is about.
        """
        if not issue_ids:
            return {}
        rows = self.session.scalars(
            select(DbIssue)
            .options(selectinload(DbIssue.publication))
            .where(DbIssue.id.in_(set(issue_ids)))
        )
        return {issue.id: issue for issue in rows}

    def mark_issue_queued(self, issue_id: int) -> None:
        """Queue *issue_id* for download and reset its retry bookkeeping.

        Every caller of this method (the manual "Retry"/"Download"
        button, ``queue_missing_issues``) represents a deliberate,
        explicit decision to try again - so it gets a fresh
        :data:`MAX_AUTO_RETRIES` budget rather than inheriting whatever
        was left over from a previous automatic retry cycle. The
        automatic backoff requeue (:meth:`requeue_due_retries`)
        deliberately does *not* go through this method, since it must
        keep counting against the same budget.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.QUEUED
            issue.retry_count = 0
            issue.next_retry_at = None

    def mark_issue_downloading(self, issue_id: int) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DOWNLOADING
            issue.progress_current = 0
            issue.progress_total = 0

    def update_issue_progress(self, issue_id: int, current: int, total: int) -> None:
        """Record live page-download progress for *issue_id*.

        Called by the downloader after each page completes so the UI can
        poll and render ``current / total pages``. Safe to call with
        ``total=0`` to clear the counters.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.progress_current = current
            issue.progress_total = total

    def mark_issue_done(
        self, issue_id: int, file_path: str, file_size: int | None = None
    ) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.DONE
            issue.file_path = file_path
            if file_size is not None:
                issue.file_size = file_size
            issue.downloaded_at = _now()
            issue.error_message = None
            issue.progress_current = 0
            issue.progress_total = 0
            issue.retry_count = 0
            issue.next_retry_at = None

    def mark_issue_error(self, issue_id: int, error: str) -> None:
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.ERROR
            issue.error_message = error
            issue.progress_current = 0
            issue.progress_total = 0

    def schedule_issue_retry(self, issue_id: int, error: str) -> bool:
        """Move *issue_id* to RETRY_PENDING if it still has attempts left.

        Called right after :meth:`mark_issue_error` for a download that
        failed with an error that looks transient (TASK-1363) - network
        blips, a locked database, a 5xx from Flipp. Once
        :data:`MAX_AUTO_RETRIES` automatic attempts have already been
        used, this does nothing and the issue is left exactly as
        ``mark_issue_error`` set it: ``ERROR``, requiring the same
        deliberate click as any other failure. Returns whether a retry
        was actually scheduled.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue is None:
            return False
        if issue.retry_count >= MAX_AUTO_RETRIES:
            return False
        issue.retry_count += 1
        delay_minutes = RETRY_DELAYS_MINUTES[issue.retry_count - 1]
        issue.status = IssueStatus.RETRY_PENDING
        issue.next_retry_at = _now() + timedelta(minutes=delay_minutes)
        issue.error_message = error
        return True

    def requeue_due_retries(self) -> int:
        """Re-queue every RETRY_PENDING issue whose backoff has elapsed.

        This is the *only* path that revives an issue scheduled by
        :meth:`schedule_issue_retry` - poll's own catch-up pass excludes
        failed issues on purpose (``include_failed=False``), so an
        automatic retry must not reuse that door. Deliberately does not
        touch ``retry_count``: the point of the counter is to remember
        how many attempts this issue has already burned through, not to
        hand it a fresh budget just for being requeued.

        Returns the number of issues requeued.
        """
        now = _now()
        due = self.session.scalars(
            select(DbIssue).where(
                DbIssue.status == IssueStatus.RETRY_PENDING,
                DbIssue.next_retry_at.is_not(None),
                DbIssue.next_retry_at <= now,
            )
        )
        requeued = 0
        for issue in due:
            issue.status = IssueStatus.QUEUED
            issue.next_retry_at = None
            self.create_job("download", {"issue_id": issue.id})
            requeued += 1
        return requeued

    def reset_issue(self, issue_id: int) -> None:
        """Clear download metadata so the issue is treated as not downloaded.

        Does not touch the file on disk – callers are expected to unlink
        the file (if desired) before calling this.
        """
        issue = self.session.get(DbIssue, issue_id)
        if issue:
            issue.status = IssueStatus.NEW
            issue.file_path = None
            issue.downloaded_at = None
            issue.error_message = None
            issue.retry_count = 0
            issue.next_retry_at = None

    # ------------------------------------------------------------------
    # Sync helper – call after a fresh API fetch
    # ------------------------------------------------------------------

    def sync_publications(
        self, api_publications: list[DomainPublication]
    ) -> list[DbIssue]:
        """Upsert all publications + issues from a fresh API response.

        Never deletes anything - a publication that drops out of the
        response keeps its download history and file paths (see
        ``_sync_delisted_publications``). Returns a list of *newly
        discovered* :class:`DbIssue` rows that the caller can queue for
        download.
        """
        new_issues: list[DbIssue] = []
        seen_codes: set[str] = set()
        for pub in api_publications:
            db_pub = self.upsert_publication(pub)
            db_pub.last_polled_at = _now()
            db_pub.delisted_at = None
            seen_codes.add(db_pub.custom_code)
            seen_issue_codes: set[str] = set()
            for issue in pub.issues:
                db_issue, created = self.upsert_issue(issue, db_pub.id)
                db_issue.delisted_at = None
                seen_issue_codes.add(db_issue.custom_code)
                if created:
                    new_issues.append(db_issue)
            if seen_issue_codes:
                self._mark_missing_issues_delisted(db_pub.id, seen_issue_codes)
        self._disable_watched_folder_collisions()
        if seen_codes:
            self._mark_missing_publications_delisted(seen_codes)
        return new_issues

    def discover_editions(
        self, publication_id: int, editions: list[DomainIssue]
    ) -> list[DbIssue]:
        """Add editions PageSuite lists that this publication lacks (TASK-1439).

        Unlike :meth:`sync_publications` this NEVER marks anything
        delisted. The PageSuite edition list is a *superset* of the Flipp
        API's - it includes the editions the app hides - so an issue
        absent from it carries no signal at all, and delisting on that
        basis would wrongly bury issues the API still lists. It only
        inserts what is missing (matched on ``custom_code`` == the
        PageSuite ``@editionguid`` == the reader ``eid``) and returns the
        newly created rows for the caller to queue.
        """
        new_issues: list[DbIssue] = []
        for issue in editions:
            db_issue, created = self.upsert_issue(issue, publication_id)
            if created:
                new_issues.append(db_issue)
        return new_issues

    def _mark_missing_publications_delisted(self, seen_codes: set[str]) -> None:
        """Mark every publication absent from *seen_codes* as delisted.

        Only called when ``seen_codes`` is non-empty (TASK-1426): an
        empty or partial API response must never be read as "everything
        disappeared". A hard failure (network error, non-2xx, ...)
        already never reaches here at all -
        :func:`flipp_dl.scheduler.poll_publications` catches
        ``FlippError`` and calls ``finish_job(error=...)`` before
        ``sync_publications`` runs. The remaining risk this guards
        against is a *successful* response that happens to be empty.

        Marked on the very first poll a publication is missing from,
        not after several misses in a row: sync_publications only runs
        against a response the caller already treated as a success, so
        "missing from a successful response" is itself the signal, not
        noise to debounce. The mark carries no meaning beyond "not
        currently listed" - the publication, its issues, and any
        downloaded files are left untouched, and the issue stays
        reachable straight through the reader API regardless.
        """
        now = _now()
        missing = self.session.scalars(
            select(DbPublication)
            .where(DbPublication.custom_code.not_in(seen_codes))
            .where(DbPublication.delisted_at.is_(None))
        )
        for db_pub in missing:
            db_pub.delisted_at = now

    def _mark_missing_issues_delisted(
        self, publication_id: int, seen_codes: set[str]
    ) -> None:
        """Mark every issue of *publication_id* absent from *seen_codes*.

        Same mechanism, and the same rationale, as
        ``_mark_missing_publications_delisted`` (TASK-1426) - just scoped
        to one publication's issue list instead of the whole publication
        set. Only called when ``seen_codes`` is non-empty: an empty or
        partial issue list for a publication that otherwise synced fine
        must never be read as "every issue of this publication vanished
        at once" - that guard is what caller (``sync_publications``)
        enforces before calling this. Codes for a delisted issue exist
        nowhere but this database once marked - a publication dropping
        1900+ issues in one poll is exactly the case that must not read
        as noise to ignore.

        Marked on the first poll an issue is missing from a *successful*
        response, not after repeated misses - "missing from a response
        the caller already treated as a success" is itself the signal.
        The mark carries no meaning beyond "not currently listed": the
        issue and any downloaded file are left untouched, and the issue
        stays reachable straight through the reader API regardless.
        """
        now = _now()
        missing = self.session.scalars(
            select(DbIssue)
            .where(DbIssue.publication_id == publication_id)
            .where(DbIssue.custom_code.not_in(seen_codes))
            .where(DbIssue.delisted_at.is_(None))
        )
        for db_issue in missing:
            db_issue.delisted_at = now

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.session.get(DbSetting, key)
        return row.value if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        row = self.session.get(DbSetting, key)
        if row is None:
            self.session.add(DbSetting(key=key, value=value))
        else:
            row.value = value

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def create_job(self, job_type: str, payload: dict | None = None) -> DbJob:
        job = DbJob(
            job_type=job_type,
            payload=json.dumps(payload or {}),
            status=JobStatus.QUEUED,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def start_job(self, job_id: int) -> None:
        job = self.session.get(DbJob, job_id)
        if job:
            job.status = JobStatus.RUNNING
            job.started_at = _now()

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        job = self.session.get(DbJob, job_id)
        if job:
            job.status = JobStatus.ERROR if error else JobStatus.DONE
            job.finished_at = _now()
            job.error_message = error

    def list_jobs(
        self,
        limit: int = 50,
        status: str | None = None,
        job_type: str | None = None,
    ) -> list[DbJob]:
        """Return the newest jobs, optionally narrowed by status/type.

        Filtering happens in the query, not on the returned page - a
        queue deeper than *limit* would otherwise be invisible behind
        newer finished jobs.
        """
        q = select(DbJob)
        if status is not None:
            q = q.where(DbJob.status == status)
        if job_type is not None:
            q = q.where(DbJob.job_type == job_type)
        q = q.order_by(DbJob.created_at.desc(), DbJob.id.desc()).limit(limit)
        return list(self.session.scalars(q))

    def count_jobs_by_status(self) -> dict[str, int]:
        """Return ``{status: count}`` over the whole jobs table.

        Every known status is present in the result, zero-filled, so
        callers can render a counter without guarding for missing keys.
        """
        rows = self.session.execute(
            select(DbJob.status, func.count(DbJob.id)).group_by(DbJob.status)
        ).all()
        counts = {
            JobStatus.QUEUED.value: 0,
            JobStatus.RUNNING.value: 0,
            JobStatus.DONE.value: 0,
            JobStatus.ERROR.value: 0,
        }
        for status, count in rows:
            counts[status] = int(count)
        return counts

    def get_job(self, job_id: int) -> DbJob | None:
        return self.session.get(DbJob, job_id)

    def get_oldest_queued_job(self, job_type: str) -> DbJob | None:
        """Return the oldest queued job of *job_type*, or ``None``.

        Queries the DB directly for the oldest match instead of
        Python-filtering a fixed-size window of recent jobs – a
        bulk-queue that pushes more than the window size of newer jobs
        would otherwise permanently hide an older queued job (TASK-1282).

        ``created_at`` isn't guaranteed millisecond resolution on
        SQLite, so ``id`` is used as a deterministic tiebreaker.
        """
        stmt = (
            select(DbJob)
            .where(DbJob.job_type == job_type, DbJob.status == JobStatus.QUEUED)
            .order_by(DbJob.created_at.asc(), DbJob.id.asc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def reset_stuck_download_jobs(self) -> int:
        """Reset RUNNING download jobs (and their issues) back to QUEUED.

        If the process crashes or is restarted mid-download, the job row
        stays RUNNING and the issue stays DOWNLOADING forever – nothing
        ever picks it up again, and the web UI's manual re-queue button
        skips issues in that state. Called once at startup.

        Returns the number of job rows reset.
        """
        stmt = select(DbJob).where(
            DbJob.job_type == "download", DbJob.status == JobStatus.RUNNING
        )
        stuck_jobs = list(self.session.scalars(stmt))
        for job in stuck_jobs:
            job.status = JobStatus.QUEUED
            job.started_at = None
            try:
                payload = json.loads(job.payload)
            except (TypeError, ValueError):
                payload = {}
            issue_id = payload.get("issue_id")
            if issue_id is not None:
                issue = self.session.get(DbIssue, int(issue_id))
                if issue is not None and issue.status == IssueStatus.DOWNLOADING:
                    issue.status = IssueStatus.QUEUED
        return len(stuck_jobs)

    def reset_stuck_jobs_of_type(self, job_type: str) -> int:
        """Reset RUNNING jobs of *job_type* back to QUEUED.

        For the job types that own no row outside the jobs table (the
        edition discovery and import runs) - a restart mid-run would
        otherwise leave a job RUNNING that nothing ever claims, and the
        progress view would spin on it forever. Both runs are safe to
        start over: ``discover_editions`` skips codes it already has.

        Returns the number of job rows reset.
        """
        stmt = select(DbJob).where(
            DbJob.job_type == job_type, DbJob.status == JobStatus.RUNNING
        )
        stuck = list(self.session.scalars(stmt))
        for job in stuck:
            job.status = JobStatus.QUEUED
            job.started_at = None
        return len(stuck)

    def merge_job_payload(self, job_id: int, updates: dict) -> None:
        """Merge *updates* into a job's JSON payload.

        Long-running jobs write their progress here so the UI can show
        movement rather than an unchanging "running".
        """
        job = self.session.get(DbJob, job_id)
        if job is None:
            return
        try:
            payload = json.loads(job.payload)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.update(updates)
        job.payload = json.dumps(payload, ensure_ascii=False)

    def latest_job_of_types(self, job_types: tuple[str, ...]) -> DbJob | None:
        """The newest job across *job_types*, whatever its status."""
        stmt = (
            select(DbJob)
            .where(DbJob.job_type.in_(job_types))
            .order_by(DbJob.created_at.desc(), DbJob.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_active_download_jobs(self) -> list[DbJob]:
        """Download jobs that are still queued or running."""
        return list(
            self.session.scalars(
                select(DbJob).where(
                    DbJob.job_type == "download",
                    DbJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
                )
            )
        )

    def release_duplicate_file_claims(self) -> int:
        """Un-mark issues that claim a file belonging to another issue.

        The oldest download keeps the file; the others never had one of
        their own, so they go back to not-downloaded and get picked up
        by the next backfill (TASK-1349).

        Returns the number of issues released.
        """
        released = 0
        for group in self.list_issues_sharing_files():
            # Whoever downloaded first owns the file; fall back to the
            # lowest id when timestamps are missing or equal.
            keeper = min(
                group,
                key=lambda i: (i.downloaded_at or datetime.max, i.id),
            )
            for issue in group:
                if issue.id == keeper.id:
                    continue
                issue.status = IssueStatus.NEW
                issue.file_path = None
                issue.downloaded_at = None
                issue.progress_current = 0
                issue.progress_total = 0
                released += 1
        return released

    def reset_orphaned_issues(self) -> int:
        """Reset issues stuck in queued/downloading with no job behind them.

        Issue status and the jobs table can drift apart - a restart at
        the wrong moment, or a job that died after the issue was marked
        queued. The row then sits there forever: nothing picks it up,
        and the UI refuses to re-queue an issue that already claims to
        be queued (TASK-1341).

        Returns the number of issues reset.
        """
        active_issue_ids: set[int] = set()
        for job in self.list_active_download_jobs():
            try:
                payload = json.loads(job.payload or "{}")
            except (TypeError, ValueError):
                continue
            issue_id = payload.get("issue_id")
            if issue_id is not None:
                try:
                    active_issue_ids.add(int(issue_id))
                except (TypeError, ValueError):
                    continue

        stuck = self.session.scalars(
            select(DbIssue).where(
                DbIssue.status.in_((IssueStatus.QUEUED, IssueStatus.DOWNLOADING))
            )
        )
        reset = 0
        for issue in stuck:
            if issue.id in active_issue_ids:
                continue
            issue.status = IssueStatus.NEW
            issue.progress_current = 0
            issue.progress_total = 0
            reset += 1
        return reset

    def queue_missing_issues(
        self,
        publication_id: int,
        *,
        include_failed: bool = True,
        since: datetime | None = None,
    ) -> int:
        """Queue issues of a publication that aren't downloaded yet.

        This is the explicit "fetch the back catalogue" action (TASK-1361)
        - watching a publication queues nothing by itself any more, and a
        poll only auto-queues issues discovered since watching started.
        Reaching into everything a publication has ever had missing is
        this method's job, triggered by the dedicated button on the
        publication's detail page.

        Issues already queued or downloading are skipped, so calling
        this repeatedly is safe. *include_failed* re-queues issues that
        previously errored: right for an explicit click, wrong for an
        automatic poll, where a permanently broken issue would come
        back every six hours.

        *since*, when given, restricts this to issues discovered on or
        after that timestamp (``DbIssue.discovered_at >= since``). This
        is how poll's catch-up pass stays inside "bevaka framåt": it
        passes the publication's ``watch_started_at`` so a back catalogue
        that predates watching is never silently pulled in (TASK-1361).
        The explicit button leaves this ``None`` - an intentional click
        is meant to reach the whole backlog.

        Returns the number of issues queued.
        """
        wanted = [IssueStatus.NEW]
        if include_failed:
            wanted.append(IssueStatus.ERROR)

        conditions = [
            DbIssue.publication_id == publication_id,
            DbIssue.status.in_(wanted),
        ]
        if since is not None:
            conditions.append(DbIssue.discovered_at >= since)

        pending = self.session.scalars(select(DbIssue).where(*conditions))
        queued = 0
        for issue in pending:
            self.mark_issue_queued(issue.id)
            self.create_job("download", {"issue_id": issue.id})
            queued += 1
        return queued

    def queue_warn_threshold_bytes(self) -> int:
        """The size, in bytes, above which a backfill confirm must warn.

        Overridable per instance: the ``queue_warn_threshold_bytes``
        setting wins if set (edited as gigabytes on the settings page),
        then the ``FLIPP_QUEUE_WARN_THRESHOLD_BYTES`` env var, else the
        5 GiB default (TASK-1361).
        """
        from_setting = _parse_positive_int(
            self.get_setting(_QUEUE_WARN_THRESHOLD_SETTING, "")
        )
        if from_setting is not None:
            return from_setting
        from_env = _parse_positive_int(os.environ.get(_QUEUE_WARN_THRESHOLD_ENV, ""))
        if from_env is not None:
            return from_env
        return _DEFAULT_QUEUE_WARN_THRESHOLD_BYTES

    def estimate_missing_download_size(
        self, publication_id: int, *, include_failed: bool = True
    ) -> QueueSizeEstimate:
        """Estimate what queuing *publication_id*'s missing issues would cost.

        Read-only and side-effect free - safe to call on every page render,
        unlike a filesystem walk. Sizes come from ``issues.file_size``,
        populated when an issue is marked done (download or disk-import
        backfill); nothing is stat()'d here.

        The per-issue size is the average of this publication's own
        downloaded issues where available (publications vary wildly in
        page count, so a pocket-sized magazine and a thin weekly need
        their own basis). When this publication has no downloaded issues
        with a known size, the median size across *all* downloaded issues
        is used instead. When neither exists, ``estimated_bytes`` is
        ``None`` - the caller must show the issue count without a
        fabricated size, never a guessed number.
        """
        wanted = [IssueStatus.NEW]
        if include_failed:
            wanted.append(IssueStatus.ERROR)

        issue_count = int(
            self.session.scalar(
                select(func.count(DbIssue.id)).where(
                    DbIssue.publication_id == publication_id,
                    DbIssue.status.in_(wanted),
                )
            )
            or 0
        )

        per_issue_bytes: float | None = None
        basis = "none"

        pub_avg = self.session.scalar(
            select(func.avg(DbIssue.file_size)).where(
                DbIssue.publication_id == publication_id,
                DbIssue.status == IssueStatus.DONE,
                DbIssue.file_size.is_not(None),
            )
        )
        if pub_avg is not None:
            per_issue_bytes = float(pub_avg)
            basis = "publication"
        else:
            sizes = list(
                self.session.scalars(
                    select(DbIssue.file_size).where(
                        DbIssue.status == IssueStatus.DONE,
                        DbIssue.file_size.is_not(None),
                    )
                )
            )
            if sizes:
                per_issue_bytes = float(statistics.median(sizes))
                basis = "global"

        estimated_bytes = (
            round(per_issue_bytes * issue_count)
            if per_issue_bytes is not None
            else None
        )
        return QueueSizeEstimate(
            issue_count=issue_count,
            estimated_bytes=estimated_bytes,
            basis=basis,
        )

    def cancel_issue(self, issue_id: int) -> int:
        """Stop an in-flight issue: reset it and finish its jobs.

        Returns the number of jobs that were marked cancelled. Used by
        the UI so a stuck row always has a way out.
        """
        cancelled = 0
        for job in self.list_active_download_jobs():
            try:
                payload = json.loads(job.payload or "{}")
            except (TypeError, ValueError):
                continue
            if payload.get("issue_id") == issue_id:
                self.finish_job(job.id, error="Cancelled from the web UI")
                cancelled += 1

        issue = self.session.get(DbIssue, issue_id)
        if issue is not None:
            issue.status = IssueStatus.NEW
            issue.progress_current = 0
            issue.progress_total = 0
        return cancelled

    def purge_old_jobs(
        self,
        *,
        max_age_days: int = 30,
        keep_min: int = 500,
    ) -> int:
        """Delete finished jobs older than *max_age_days*.

        The newest *keep_min* finished jobs are always retained so a
        busy instance still has recent history for the Jobs page even
        if it just finished a big bulk-download. Running or queued
        jobs are never purged – they either finish and become eligible
        later, or they stay forever if the process crashed with stuck
        rows (which is the user's cue to investigate).

        Returns the number of rows removed.
        """
        terminal = (JobStatus.DONE, JobStatus.ERROR)
        cutoff = _now() - timedelta(days=max_age_days)

        keep_ids: set[int] = set()
        if keep_min > 0:
            keep_ids = set(
                self.session.scalars(
                    select(DbJob.id)
                    .where(DbJob.status.in_(terminal))
                    .order_by(DbJob.created_at.desc())
                    .limit(keep_min)
                )
            )

        stmt = delete(DbJob).where(
            DbJob.status.in_(terminal),
            DbJob.created_at < cutoff,
        )
        if keep_ids:
            stmt = stmt.where(~DbJob.id.in_(keep_ids))

        result = self.session.execute(stmt)
        return int(result.rowcount or 0)
