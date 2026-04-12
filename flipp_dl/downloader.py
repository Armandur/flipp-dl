"""Download an issue from Flipp and merge it into a single PDF file."""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .api import FlippClient
from .models import Issue, Publication
from .storage import issue_path, publication_folder

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 4


class IssueDownloader:
    """Download and persist whole issues as merged PDF files."""

    def __init__(
        self,
        client: FlippClient,
        output_root: Path,
        *,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        self.client = client
        self.output_root = Path(output_root)
        self.workers = max(1, workers)

    # ------------------------------------------------------------------

    def download_issue(
        self, publication: Publication, issue: Issue, *, skip_existing: bool = True
    ) -> Path:
        """Download *issue* and return the resulting file path.

        Pages are fetched in parallel (up to ``self.workers`` at a time)
        but written to the final PDF in their original order.
        """
        target = issue_path(self.output_root, publication, issue)

        if skip_existing and target.is_file():
            logger.info("Skipping existing file: %s", target)
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

        pages = self._fetch_pages_parallel(pdf_urls)

        writer = PdfWriter()
        try:
            for data in pages:
                writer.append(PdfReader(io.BytesIO(data)))
            with target.open("wb") as fh:
                writer.write(fh)
        finally:
            writer.close()

        logger.info("Wrote %s", target)
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

    def _fetch_pages_parallel(self, pdf_urls: list[str]) -> list[bytes]:
        """Fetch all page URLs concurrently, preserving order."""
        if not pdf_urls:
            return []
        if self.workers == 1 or len(pdf_urls) == 1:
            return [self.client.download_pdf(url) for url in pdf_urls]

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            return list(executor.map(self.client.download_pdf, pdf_urls))
