"""Command line entry point for flipp-dl."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .api import FlippClient, FlippError
from .config import default_output_path, load_token
from .db.session import make_session_factory
from .downloader import DEFAULT_WORKERS, IssueDownloader
from .models import Category, Publication

# Serietidningar – kept as the default so existing users get the same
# behaviour if they run `python app.py` without arguments.
DEFAULT_CATEGORY_ID = 52
DEFAULT_DB_PATH = Path("flipp.db")

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
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        metavar="PATH",
        help=f"SQLite database file (default: {DEFAULT_DB_PATH}).",
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
        "--import-existing",
        action="store_true",
        help=(
            "Reconcile the database against files already in --output "
            "(no network calls), print a summary, and exit."
        ),
    )
    parser.add_argument(
        "--migrate-filenames",
        action="store_true",
        help=(
            "Rename downloaded files to the current OS-safe naming scheme, "
            "update their database paths, print a summary, and exit."
        ),
    )

    # Scheduler mode
    scheduler_group = parser.add_argument_group("scheduler mode")
    scheduler_group.add_argument(
        "--scheduler",
        action="store_true",
        help=(
            "Run as a long-lived scheduler: poll Flipp for new issues "
            "and download watched publications automatically."
        ),
    )
    scheduler_group.add_argument(
        "--poll-interval",
        type=int,
        default=360,
        metavar="MINUTES",
        help="How often to poll for new issues (default: 360 min = 6 h).",
    )
    scheduler_group.add_argument(
        "--download-interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="How often to check the download queue (default: 30 s).",
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


def _print_import_report(report) -> None:
    print(f"Backfilled {len(report.backfilled)} issue(s) already on disk.")
    for item in report.backfilled:
        print(
            f"  done: {item['publication']} / {item['issue_name']} -> {item['file_path']}"
        )

    print(f"Orphan file(s) with no matching issue: {len(report.orphan_files)}")
    for path in report.orphan_files:
        print(f"  orphan: {path}")

    print(f"Issue(s) marked done but missing on disk: {len(report.missing_files)}")
    for item in report.missing_files:
        print(
            f"  missing: {item['publication']} / {item['issue_name']} -> {item['file_path']}"
        )

    print(f"File(s) shared by more than one issue: {len(report.shared_files)}")
    for group in report.shared_files:
        names = ", ".join(
            f"{i['publication']} / {i['issue_name']} ({i['status']})"
            for i in group["issues"]
        )
        print(f"  shared: {group['file_path']} <- {names}")

    if not report.has_findings:
        print("No drift found - the database and the output folder agree.")


def _run_import_existing(args: argparse.Namespace) -> int:
    """Reconcile the DB against --output without touching the network."""
    from .db.repository import DownloadRepository
    from .db.session import get_session

    output_root = args.output or default_output_path()
    session_factory = make_session_factory(args.db)
    with get_session(session_factory) as session:
        report = DownloadRepository(session).import_existing_files(output_root)
    _print_import_report(report)
    return 0


@dataclass
class _FilenameMigrationReport:
    renamed: list[tuple[str, str]] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _print_migration_items(title: str, label: str, items: list[str]) -> None:
    print(f"{title}: {len(items)}")
    for item in items:
        print(f"  {label}: {item}")


def _print_filename_migration_report(report: _FilenameMigrationReport) -> None:
    print(f"Renamed file(s): {len(report.renamed)}")
    for source, target in report.renamed:
        print(f"  renamed: {source} -> {target}")
    _print_migration_items(
        "Recovered database path(s) after an earlier move",
        "recovered",
        report.recovered,
    )
    print(f"Already using the current filename: {report.unchanged}")
    _print_migration_items("Missing source file(s)", "missing", report.missing)
    _print_migration_items(
        "Naming conflict(s), left unchanged", "conflict", report.conflicts
    )
    _print_migration_items("Migration error(s), left unchanged", "error", report.errors)


def _migration_source(output_root: Path, file_path: str) -> Path | None:
    raw = Path(file_path)
    try:
        source = (raw if raw.is_absolute() else output_root / raw).resolve()
        source.relative_to(output_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return source


def _run_filename_migration(args: argparse.Namespace) -> int:
    from collections import Counter

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from . import storage
    from .db.models import DbIssue
    from .db.session import get_session

    output_root = (args.output or default_output_path()).resolve()
    session_factory = make_session_factory(args.db)
    report = _FilenameMigrationReport()

    with get_session(session_factory) as session:
        issues = list(
            session.scalars(
                select(DbIssue)
                .options(selectinload(DbIssue.publication))
                .where(DbIssue.file_path.is_not(None))
                .order_by(DbIssue.id)
            )
        )
        plans: list[tuple[DbIssue, Path | None, Path]] = []
        for issue in issues:
            assert issue.file_path is not None
            source = _migration_source(output_root, issue.file_path)
            disambiguation_tail = storage.safe_name(f" ({issue.custom_code[:8]}).pdf")
            disambiguate = Path(issue.file_path).name.endswith(disambiguation_tail)
            target = storage.issue_path(
                output_root,
                issue.publication,
                issue,
                disambiguate=disambiguate,
            )
            plans.append((issue, source, target))

        source_counts = Counter(source for _, source, _ in plans if source is not None)
        target_counts = Counter(target for _, _, target in plans)

        for issue, source, target in plans:
            stored_path = issue.file_path or ""
            if source is None:
                report.missing.append(stored_path)
                continue
            if source_counts[source] > 1:
                report.conflicts.append(f"several issues use the source path {source}")
                continue
            if target_counts[target] > 1:
                report.conflicts.append(
                    f"several issues would use the target path {target}"
                )
                continue

            if source.is_file():
                if source == target:
                    if issue.file_path != str(target):
                        issue.file_path = str(target)
                        report.recovered.append(str(target))
                    else:
                        report.unchanged += 1
                    continue
                if target.exists():
                    try:
                        same_file = source.samefile(target)
                    except OSError:
                        same_file = False
                    if same_file:
                        issue.file_path = str(target)
                        report.recovered.append(str(target))
                    else:
                        report.conflicts.append(f"target already exists: {target}")
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(target)
                except OSError as exc:
                    report.errors.append(f"{source} -> {target}: {exc}")
                    continue
                issue.file_path = str(target)
                report.renamed.append((str(source), str(target)))
                continue

            if target.is_file():
                issue.file_path = str(target)
                report.recovered.append(str(target))
            else:
                report.missing.append(str(source))

    _print_filename_migration_report(report)
    return 1 if report.conflicts or report.errors else 0


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    if args.import_existing:
        return _run_import_existing(args)
    if args.migrate_filenames:
        return _run_filename_migration(args)

    # ------------------------------------------------------------------
    # Scheduler mode – hand off and block
    # ------------------------------------------------------------------
    if args.scheduler:
        from .scheduler import run_scheduler

        token = args.token or load_token()
        if not token:
            logger.error(
                "No Flipp token found. Pass --token, set FLIPP_TOKEN or "
                "create a `token` file. See README.md for details."
            )
            return 2
        # Temporarily inject token override into env so scheduler picks it up.
        if args.token:
            import os

            os.environ["FLIPP_TOKEN"] = args.token

        run_scheduler(
            args.db,
            poll_interval_minutes=args.poll_interval,
            download_interval_seconds=args.download_interval,
            workers=args.workers,
            output_root=args.output,
        )
        return 0  # only reached on clean shutdown

    # ------------------------------------------------------------------
    # One-shot download mode
    # ------------------------------------------------------------------
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
    session_factory = make_session_factory(args.db)
    from .db.repository import DownloadRepository
    from .db.session import get_session

    with get_session(session_factory) as session:
        repo = DownloadRepository(session)
        repo.sync_publications(selected)

    downloader = IssueDownloader(
        client,
        output_root,
        workers=args.workers,
        repository=DownloadRepository(session_factory()),
    )
    for publication in selected:
        downloader.download_publication(
            publication, skip_existing=not args.no_skip_existing
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
