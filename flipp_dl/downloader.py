"""Download an issue from Flipp and merge it into a single PDF file."""

from __future__ import annotations

import io
import logging
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from pypdf import PdfReader, PdfWriter
from sqlalchemy.exc import OperationalError

from .api import FlippClient
from .models import Issue, Publication
from .storage import issue_path, publication_folder

if TYPE_CHECKING:
    from .db.repository import DownloadRepository

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 4

# Minimum seconds between progress writes during a download.
PROGRESS_INTERVAL_SECONDS = 1.0

# How many leading pages a preview fetches - enough to judge the issue
# without paying for a full download's worth of bandwidth.
DEFAULT_PREVIEW_PAGES = 3

# How long a preview file is allowed to sit in the temp directory before
# a purge sweep is entitled to remove it (TASK-1344).
PREVIEW_MAX_AGE_SECONDS = 600


def default_preview_root() -> Path:
    """Where preview PDFs live when the caller doesn't pick a location.

    Deliberately the system temp dir, never under ``output_root``: the
    Library view and the disk importer (``import_existing_files``) walk
    ``output_root`` and would otherwise mistake a preview for a real
    download.
    """
    return Path(tempfile.gettempdir()) / "flipp-dl-previews"


def purge_old_previews(
    preview_root: Path | None = None,
    *,
    max_age_seconds: int = PREVIEW_MAX_AGE_SECONDS,
) -> int:
    """Delete preview PDFs older than *max_age_seconds*.

    Mirrors ``DownloadRepository.purge_old_jobs`` - a cheap sweep meant
    to be called opportunistically from the poll tick, not a background
    thread of its own. Returns the number of files removed.
    """
    root = Path(preview_root) if preview_root is not None else default_preview_root()
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in root.glob("preview-*.pdf"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class IssueDownloader:
    """Download and persist whole issues as merged PDF files.

    Pass a :class:`~flipp_dl.db.repository.DownloadRepository` to have
    the downloader record status (downloading / done / error) in the
    database as it works.
    """

    def __init__(
        self,
        client: FlippClient,
        output_root: Path,
        *,
        workers: int = DEFAULT_WORKERS,
        repository: DownloadRepository | None = None,
    ) -> None:
        self.client = client
        self.output_root = Path(output_root)
        self.workers = max(1, workers)
        self.repository = repository

    # ------------------------------------------------------------------

    def download_issue(
        self, publication: Publication, issue: Issue, *, skip_existing: bool = True
    ) -> Path:
        """Download *issue* and return the resulting file path.

        Pages are fetched in parallel (up to ``self.workers`` at a time)
        but written to the final PDF in their original page order.

        If a :class:`~flipp_dl.db.repository.DownloadRepository` was
        supplied, the issue's DB status is kept up-to-date throughout.
        """
        db_issue_id = self._resolve_db_id(publication, issue)
        target = self._target_path(publication, issue, db_issue_id)

        if skip_existing and target.is_file():
            # The file is ours (see _target_path), so the issue really is
            # downloaded - record that instead of returning with the
            # status untouched, which used to leave rows stuck in queued.
            logger.info("Skipping existing file: %s", target)
            if db_issue_id is not None and self.repository is not None:
                try:
                    size = target.stat().st_size
                except OSError:
                    size = None
                self.repository.mark_issue_done(
                    db_issue_id, str(target), file_size=size
                )
                self.repository.session.commit()
            return target

        target.parent.mkdir(parents=True, exist_ok=True)

        pdf_urls = self.client.fetch_issue_pdf_urls(
            publication.custom_code, issue.custom_code
        )
        logger.info(
            "Downloading %s / %s (%d pages, %d workers)",
            publication.name,
            issue.issue_name,
            len(pdf_urls),
            self.workers,
        )

        if db_issue_id is not None and self.repository is not None:
            self.repository.mark_issue_downloading(db_issue_id)
            self._write_progress(db_issue_id, 0, len(pdf_urls))

        progress_cb: Callable[[int, int], None] | None = None
        if db_issue_id is not None and self.repository is not None:
            issue_id = db_issue_id
            last_write = 0.0

            def progress_cb(done: int, total: int) -> None:
                # The counter only feeds a UI label, so throttle it: one
                # write per page meant hundreds of commits competing with
                # the web thread for the SQLite write lock. The final page
                # always writes so the UI doesn't stop short of the total.
                nonlocal last_write
                now = time.monotonic()
                if done < total and now - last_write < PROGRESS_INTERVAL_SECONDS:
                    return
                last_write = now
                self._write_progress(issue_id, done, total)

        try:
            pages = self._fetch_pages_parallel(pdf_urls, progress_cb=progress_cb)

            writer = PdfWriter()
            try:
                for data in pages:
                    writer.append(PdfReader(io.BytesIO(data)))
                with target.open("wb") as fh:
                    writer.write(fh)
            finally:
                writer.close()

        except Exception as exc:
            if db_issue_id is not None and self.repository is not None:
                self.repository.mark_issue_error(db_issue_id, str(exc))
                self.repository.session.commit()
            raise

        if db_issue_id is not None and self.repository is not None:
            try:
                size = target.stat().st_size
            except OSError:
                size = None
            self.repository.mark_issue_done(db_issue_id, str(target), file_size=size)
            self.repository.session.commit()

        logger.info("Wrote %s", target)
        return target

    def preview_issue(
        self,
        publication: Publication,
        issue: Issue,
        *,
        pages: int = DEFAULT_PREVIEW_PAGES,
        preview_root: Path | None = None,
    ) -> Path:
        """Fetch the first *pages* pages of *issue* to a throwaway file.

        Deliberately a separate path from :meth:`download_issue`: it
        never touches ``self.output_root`` (so the file can't be mistaken
        for a real download by the Library view or the disk importer),
        never calls ``_target_path``/``skip_existing``, and never reports
        status through ``self.repository`` - a preview must not make the
        issue look queued, downloading, or done.

        Returns the path to the merged preview PDF, written under
        *preview_root* (default: :func:`default_preview_root`).
        """
        root = (
            Path(preview_root) if preview_root is not None else default_preview_root()
        )
        root.mkdir(parents=True, exist_ok=True)

        pdf_urls = self.client.fetch_issue_pdf_urls(
            publication.custom_code, issue.custom_code
        )
        subset = pdf_urls[:pages] if pages > 0 else pdf_urls
        logger.info(
            "Previewing %s / %s (%d of %d pages)",
            publication.name,
            issue.issue_name,
            len(subset),
            len(pdf_urls),
        )
        pages_data = self._fetch_pages_parallel(subset)

        target = root / f"preview-{issue.custom_code}-{uuid.uuid4().hex[:8]}.pdf"
        writer = PdfWriter()
        try:
            for data in pages_data:
                writer.append(PdfReader(io.BytesIO(data)))
            with target.open("wb") as fh:
                writer.write(fh)
        finally:
            writer.close()

        logger.info("Wrote preview %s", target)
        return target

    def download_publication(
        self, publication: Publication, *, skip_existing: bool = True
    ) -> list[Path]:
        """Download every issue of *publication*. Returns written paths."""
        publication_folder(self.output_root, publication).mkdir(
            parents=True, exist_ok=True
        )
        written: list[Path] = []
        for issue in publication.issues:
            try:
                written.append(
                    self.download_issue(publication, issue, skip_existing=skip_existing)
                )
            except Exception as exc:  # noqa: BLE001 - log and continue
                logger.error(
                    "Failed to download %s / %s: %s",
                    publication.name,
                    issue.issue_name,
                    exc,
                )
        return written

    # ------------------------------------------------------------------

    def _target_path(
        self, publication: Publication, issue: Issue, db_issue_id: int | None
    ) -> Path:
        """Where this issue's PDF belongs, avoiding another issue's file.

        Publication, date and issue name do not identify an issue: Flipp
        publishes distinct issues sharing all three. Without this check
        the second one silently adopts the first one's file and its own
        content is never stored (TASK-1349).
        """
        target = issue_path(self.output_root, publication, issue)
        if self.repository is None or db_issue_id is None:
            return target

        owner = self.repository.get_issue_by_file_path(str(target))
        if owner is not None and owner.id != db_issue_id:
            unique = issue_path(self.output_root, publication, issue, disambiguate=True)
            logger.info(
                "Filename taken by issue %s - using %s instead",
                owner.custom_code,
                unique.name,
            )
            return unique
        return target

    def _write_progress(self, issue_id: int, done: int, total: int) -> None:
        """Persist the page counter, tolerating a busy database.

        A lost progress update is cosmetic; letting it propagate is not.
        The exception poisons the session, so every later write in the
        same download fails too and a finished PDF gets recorded as an
        error (TASK-1340).
        """
        if self.repository is None:
            return
        try:
            self.repository.update_issue_progress(issue_id, done, total)
            self.repository.session.commit()
        except OperationalError as exc:
            self.repository.session.rollback()
            logger.warning(
                "Could not record progress %d/%d for issue %d: %s",
                done,
                total,
                issue_id,
                exc,
            )

    def _resolve_db_id(self, publication: Publication, issue: Issue) -> int | None:
        """Return the DB id for *issue* if a repository is wired up."""
        if self.repository is None:
            return None
        db_pub = self.repository.get_publication(publication.custom_code)
        if db_pub is None:
            return None
        db_issue = self.repository.get_issue_by_code(issue.custom_code, db_pub.id)
        return db_issue.id if db_issue else None

    def _fetch_pages_parallel(
        self,
        pdf_urls: list[str],
        *,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> list[bytes]:
        """Fetch all page URLs concurrently, preserving order.

        If *progress_cb* is supplied it is invoked as ``cb(done, total)``
        after each page completes so the caller can update live
        progress counters. Unlike :meth:`ThreadPoolExecutor.map` this
        uses :func:`as_completed` so progress advances with whichever
        page finishes first rather than waiting in submission order.
        """
        total = len(pdf_urls)
        if not pdf_urls:
            return []
        if self.workers == 1 or len(pdf_urls) == 1:
            pages: list[bytes] = []
            for url in pdf_urls:
                pages.append(self.client.download_pdf(url))
                if progress_cb is not None:
                    progress_cb(len(pages), total)
            return pages

        results: list[bytes | None] = [None] * total
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            future_to_idx = {
                executor.submit(self.client.download_pdf, url): idx
                for idx, url in enumerate(pdf_urls)
            }
            done_count = 0
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()
                done_count += 1
                if progress_cb is not None:
                    progress_cb(done_count, total)
        return [r for r in results if r is not None]
