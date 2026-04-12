"""Command line entry point for flipp-dl."""

from __future__ import annotations

import logging
import sys

from .api import FlippClient, FlippError
from .config import default_output_path, load_token
from .downloader import IssueDownloader
from .models import Publication

# Serietidningar. Will become a CLI flag once P2 lands.
DEFAULT_CATEGORY_ID = 52

logger = logging.getLogger("flipp_dl")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _filter_by_category(
    publications: list[Publication], category_id: int
) -> list[Publication]:
    return [p for p in publications if p.has_category(category_id)]


def main(argv: list[str] | None = None) -> int:
    _configure_logging()

    token = load_token()
    if not token:
        logger.error(
            "No Flipp token found. Set FLIPP_TOKEN or create a `token` "
            "file next to app.py. See README.md for details."
        )
        return 2

    client = FlippClient(token)
    try:
        publications = client.fetch_publications()
    except FlippError as exc:
        logger.error("Failed to fetch publications: %s", exc)
        return 1

    selected = _filter_by_category(publications, DEFAULT_CATEGORY_ID)
    # Publications with the most issues first.
    selected.sort(key=lambda p: p.num_issues, reverse=True)
    logger.info(
        "Selected %d publications in category %d",
        len(selected),
        DEFAULT_CATEGORY_ID,
    )

    downloader = IssueDownloader(client, default_output_path())
    for publication in selected:
        downloader.download_publication(publication)

    return 0


if __name__ == "__main__":
    sys.exit(main())
