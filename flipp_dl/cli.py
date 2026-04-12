"""Command line entry point for flipp-dl."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from pathlib import Path

from .api import FlippClient, FlippError
from .config import default_output_path, load_token
from .downloader import DEFAULT_WORKERS, IssueDownloader
from .models import Category, Publication

# Serietidningar – kept as the default so existing users get the same
# behaviour if they run `python app.py` without arguments.
DEFAULT_CATEGORY_ID = 52

logger = logging.getLogger("flipp_dl")


# ----------------------------------------------------------------------
# Argument parser
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flipp_dl",
        description="Download publications from Flipp as PDF.",
    )
    parser.add_argument(
        "--token",
        help=(
            "Flipp API token. Overrides FLIPP_TOKEN env var and the "
            "`token` file next to app.py."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output directory (default: ./Output).",
    )
    parser.add_argument(
        "--category",
        type=int,
        action="append",
        default=None,
        metavar="ID",
        help=(
            "Category ID to include. Can be given multiple times. "
            f"Default when neither --category nor --publication is set: "
            f"{DEFAULT_CATEGORY_ID} (Serietidningar)."
        ),
    )
    parser.add_argument(
        "--publication",
        action="append",
        default=None,
        metavar="CODE",
        help=(
            "Publication custom code to download. Can be given multiple "
            "times. Overrides --category when set."
        ),
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List available categories and exit.",
    )
    parser.add_argument(
        "--list-publications",
        action="store_true",
        help="List available publications (optionally filtered by --category) and exit.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel page downloads per issue (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download issues even if the target file already exists.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for DEBUG).",
    )
    return parser


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _configure_logging(verbosity: int) -> None:
    level = logging.DEBUG if verbosity > 0 else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def filter_publications(
    publications: Iterable[Publication],
    *,
    category_ids: list[int] | None = None,
    publication_codes: list[str] | None = None,
) -> list[Publication]:
    """Filter *publications* by category and/or explicit code list.

    ``publication_codes`` takes precedence. If neither filter is given,
    all publications are returned.
    """
    if publication_codes:
        wanted = set(publication_codes)
        return [p for p in publications if p.custom_code in wanted]
    if category_ids:
        wanted = set(category_ids)
        return [p for p in publications if any(c.id in wanted for c in p.categories)]
    return list(publications)


def _unique_categories(publications: Iterable[Publication]) -> list[Category]:
    seen: dict[int, Category] = {}
    for publication in publications:
        for category in publication.categories:
            seen.setdefault(category.id, category)
    return sorted(seen.values(), key=lambda c: (c.name, c.id))


def _print_categories(publications: list[Publication]) -> None:
    categories = _unique_categories(publications)
    if not categories:
        print("No categories found.")
        return
    width = max(len(str(c.id)) for c in categories)
    for category in categories:
        print(f"{str(category.id).rjust(width)}  {category.name}")


def _print_publications(publications: list[Publication]) -> None:
    if not publications:
        print("No publications found.")
        return
    for publication in sorted(publications, key=lambda p: p.num_issues, reverse=True):
        cats = ", ".join(c.name for c in publication.categories) or "-"
        print(
            f"{publication.custom_code:<24}  "
            f"{publication.num_issues:>4} issues  "
            f"{publication.name}  [{cats}]"
        )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    token = args.token or load_token()
    if not token:
        logger.error(
            "No Flipp token found. Pass --token, set FLIPP_TOKEN or "
            "create a `token` file next to app.py. See README.md for "
            "details."
        )
        return 2

    client = FlippClient(token)
    try:
        publications = client.fetch_publications()
    except FlippError as exc:
        logger.error("Failed to fetch publications: %s", exc)
        return 1

    if args.list_categories:
        _print_categories(publications)
        return 0

    category_ids = args.category
    publication_codes = args.publication

    if args.list_publications:
        filtered = filter_publications(
            publications,
            category_ids=category_ids,
            publication_codes=publication_codes,
        )
        _print_publications(filtered)
        return 0

    if not publication_codes and not category_ids:
        category_ids = [DEFAULT_CATEGORY_ID]
        logger.info(
            "No --category or --publication given, defaulting to category %d",
            DEFAULT_CATEGORY_ID,
        )

    selected = filter_publications(
        publications,
        category_ids=category_ids,
        publication_codes=publication_codes,
    )
    selected.sort(key=lambda p: p.num_issues, reverse=True)

    if not selected:
        logger.warning("No publications matched the given filters.")
        return 0

    logger.info("Selected %d publications", len(selected))

    output_root = args.output or default_output_path()
    downloader = IssueDownloader(client, output_root, workers=args.workers)
    for publication in selected:
        downloader.download_publication(
            publication, skip_existing=not args.no_skip_existing
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
