"""FastAPI route definitions for flipp-dl web UI."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .. import storage
from ..api import WRONG_CREDENTIALS, FlippClient, FlippError
from ..config import load_token
from ..db.models import IssueStatus, JobStatus
from ..db.repository import (
    KOMGA_BACKFILL_LIMIT,
    DownloadRepository,
    PublicationDestinationError,
    PublicationFolderConflict,
    PublicationFolderError,
    PublicationFolderMoveError,
    default_cover_cache_root,
    fetch_and_cache_cover,
    find_cached_cover,
)
from ..db.session import get_session
from ..downloader import IssueDownloader
from ..komga import KomgaClient, KomgaError
from ..models import Issue as DomainIssue
from ..models import Publication as DomainPublication
from ..notify import NotifyError, NtfyChannel
from ..scheduler import (
    poll_publications,
    resolve_current_token,
    resolve_komga_settings_from_repo,
)
from . import i18n, opds
from .auth import auth_enabled, check_csrf_form, generate_csrf_token, verify_password

logger = logging.getLogger(__name__)


def _mark_for_translation(message: str) -> str:
    """Return *message* unchanged - marks it for ``pybabel extract`` only.

    The actual translation happens later, at request time, via
    :func:`flipp_dl.web.i18n.translate` (the language isn't known yet at
    module load time). This mirrors ``gettext_noop``/``gettext_lazy`` from
    other frameworks: pybabel's python extractor recognises a call named
    ``_`` (aliased below) and pulls its argument into the catalog even
    though nothing is translated at the call site itself.
    """
    return message


_ = _mark_for_translation

_INVALID_FOLDER_NAME = _("Use only characters that are valid in a folder name.")
_FOLDER_NAME_CONFLICT = _("That folder name is already used by another publication.")
_FOLDER_MOVE_FAILED = _("The downloaded files could not be moved.")
_NTFY_TEST_SENT = _("Test notification sent. Check your ntfy app.")
_NTFY_TEST_NO_TOPIC = _("Fill in the ntfy server URL and topic first.")
_NTFY_TEST_FAILED = _("The notification could not be delivered.")
_LOGIN_WRONG_CREDENTIALS = _("Wrong email address or password.")
_LOGIN_FAILED = _("Could not sign in to Flipp.")
_LOGIN_SAVED = _("Signed in. The token is saved and in use.")
_POLL_NEEDS_TOKEN = _("No Flipp token is configured, so there is nothing to poll with.")
_DESTINATION_CHANGE_BLOCKED = _(
    "Destination cannot be changed because this publication already has downloaded issues."
)
_SECONDARY_ROOT_MISSING = _(
    "That folder does not exist. Create it first, or pick one with the browse button."
)

# Short, non-technical messages for the "Test connection" button
# (komga_test_connection below), keyed by KomgaError.reason. The
# technical requests/urllib3 text stays out of the UI - it's logged
# instead (see the `logger.warning` call at the raise site) - so
# troubleshooting still has it even though the user only sees this.
_KOMGA_TEST_CONNECTION_MESSAGES: dict[str, str] = {
    "unreachable": _(
        "the address did not respond. Check that the URL is correct and "
        "that Komga is running."
    ),
    "auth": _("the login was rejected. Check the username, password or API key."),
    "bad_response": _(
        "the address responded, but the content doesn't look like it came "
        "from Komga."
    ),
    "other": _("something went wrong. Check the address and try again."),
}


def _repo(request: Request) -> DownloadRepository:
    session = request.app.state.session_factory()
    return DownloadRepository(session)


def _templates(request: Request):
    return request.app.state.templates


def _safe_output_file(output_root: Path, candidate: str | None) -> Path | None:
    """Resolve *candidate* under *output_root* - see storage.resolve_safe_path."""
    return storage.resolve_safe_path(output_root, candidate)


def _safe_cover_file(candidate: str | None) -> Path | None:
    """Resolve a cached-cover filename under the cover cache root.

    ``candidate`` is a filename this code generated itself
    (``fetch_and_cache_cover``'s return value, stored verbatim in the
    DB) rather than user input, but it still goes through the same
    containment guard as ``output_root`` files - cheap insurance against
    a stray ``../`` ever making it into the column.
    """
    return storage.resolve_safe_path(default_cover_cache_root(), candidate)


def _annotate_file_exists(issues, output_root: Path) -> None:
    """Attach a ``file_exists`` boolean to each ORM issue for the template."""
    for issue in issues:
        issue.file_exists = _safe_output_file(output_root, issue.file_path) is not None


def _komga_status(repo: DownloadRepository, pub) -> dict:
    """Komga mapping status for the publication detail header (TASK-1327).

    ``enabled`` is False (and the rest omitted) when Komga isn't turned
    on at all - nothing useful to show, so the template renders nothing.
    Otherwise ``mapped`` distinguishes "synkad ✓ · serie #<id>" (with a
    link into Komga) from "okänd - söker nästa gång" for a publication
    the folder-name lookup hasn't matched yet.
    """
    settings = resolve_komga_settings_from_repo(repo)
    if not settings["enabled"] or not settings["url"]:
        return {"enabled": False}
    if pub.komga_series_id:
        return {
            "enabled": True,
            "mapped": True,
            "series_id": pub.komga_series_id,
            "series_url": f"{settings['url'].rstrip('/')}/series/{pub.komga_series_id}",
        }
    return {"enabled": True, "mapped": False}


def register(app: FastAPI) -> None:
    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        """Prometheus text-exposition metrics.

        Requires login like every other route when FLIPP_PASSWORD is
        set, unless FLIPP_METRICS_PUBLIC is on - a scraper cannot log
        in, so that flag is what makes the endpoint usable. All counts
        are computed in the DB, never by loading rows: this is a poll
        target.
        """
        repo = _repo(request)
        try:
            issue_counts = repo.count_issues_by_status()
            job_counts = repo.count_jobs_by_status()
            total_pubs, watched_pubs = repo.count_publications()
        finally:
            repo.session.close()

        lines: list[str] = []

        lines.append("# HELP flipp_issues_total Number of issues by status.")
        lines.append("# TYPE flipp_issues_total gauge")
        for status, count in issue_counts.items():
            lines.append(f'flipp_issues_total{{status="{status}"}} {count}')

        lines.append("# HELP flipp_jobs_total Number of jobs by status.")
        lines.append("# TYPE flipp_jobs_total gauge")
        for status, count in job_counts.items():
            lines.append(f'flipp_jobs_total{{status="{status}"}} {count}')

        lines.append(
            "# HELP flipp_publications_total Total number of known publications."
        )
        lines.append("# TYPE flipp_publications_total gauge")
        lines.append(f"flipp_publications_total {total_pubs}")

        lines.append(
            "# HELP flipp_publications_watched Number of watched publications."
        )
        lines.append("# TYPE flipp_publications_watched gauge")
        lines.append(f"flipp_publications_watched {watched_pubs}")

        body = "\n".join(lines) + "\n"
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------------
    # Language
    # ------------------------------------------------------------------

    @app.get("/language/{lang}", include_in_schema=False)
    async def set_language(request: Request, lang: str, next: str = "/"):
        """Switch the UI language for this session.

        A plain GET is fine here (no CSRF check): this only changes a
        display preference in the session, not any stored data, so it
        carries none of the risk a state-changing POST would.
        """
        i18n.set_language(request, lang)
        safe_next = next if next.startswith("/") else "/"
        return RedirectResponse(url=safe_next, status_code=302)

    # ------------------------------------------------------------------
    # Auth – login / logout
    # ------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request, next: str = "/"):
        csrf = generate_csrf_token(request)
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            {"csrf_token": csrf, "next": next, "error": None},
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login_post(
        request: Request,
        password: str = Form(""),
        next: str = Form("/"),
    ):
        if not await check_csrf_form(request):
            return _templates(request).TemplateResponse(
                request,
                "login.html",
                {
                    "csrf_token": generate_csrf_token(request),
                    "next": next,
                    "error": "Invalid request. Please try again.",
                },
                status_code=400,
            )

        if verify_password(password):
            request.session["authenticated"] = True
            # Redirect to the original destination, but only to local paths
            safe_next = next if next.startswith("/") else "/"
            return RedirectResponse(url=safe_next, status_code=302)

        return _templates(request).TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": generate_csrf_token(request),
                "next": next,
                "error": "Incorrect password.",
            },
            status_code=401,
        )

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=302)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _dashboard_stats(repo: DownloadRepository) -> dict:
        """Counts for the dashboard cards, all computed in the DB.

        The cards poll this every few seconds, so nothing here may load
        whole tables - a live instance has tens of thousands of issues.
        """
        total_pubs, watched_pubs = repo.count_publications()
        issue_counts = repo.count_issues_by_status()
        job_counts = repo.count_jobs_by_status()
        return {
            "total_pubs": total_pubs,
            "watched_pubs": watched_pubs,
            "total_issues": sum(issue_counts.values()),
            "downloaded_issues": issue_counts[IssueStatus.DONE.value],
            # Jobs parked in retry_pending are pending work too, just
            # waiting out a backoff - counting them as queued keeps the
            # card from understating what is still coming.
            "queued_jobs": (
                job_counts[JobStatus.QUEUED.value]
                + job_counts[JobStatus.RETRY_PENDING.value]
            ),
            "running_jobs": job_counts[JobStatus.RUNNING.value],
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        repo = _repo(request)
        try:
            recent_issues = repo.list_recent_downloads(limit=10)
            _annotate_file_exists(recent_issues, request.app.state.output_root)
            recent_jobs = repo.list_jobs(limit=10)
            stats = _dashboard_stats(repo)
            # Render inside the try-block so the session is still open while
            # Jinja resolves any lazy-loaded attributes on ORM instances.
            return _templates(request).TemplateResponse(
                request,
                "dashboard.html",
                {
                    "stats": stats,
                    "recent_issues": recent_issues,
                    "recent_jobs": recent_jobs,
                    "auth_enabled": auth_enabled(),
                },
            )
        finally:
            repo.session.close()

    @app.get("/stats/cards", response_class=HTMLResponse)
    async def dashboard_stats_partial(request: Request):
        """Stats cards on their own - polled by HTMX from the dashboard."""
        repo = _repo(request)
        try:
            return _templates(request).TemplateResponse(
                request, "stats_cards.html", {"stats": _dashboard_stats(repo)}
            )
        finally:
            repo.session.close()

    # ------------------------------------------------------------------
    # Publications
    # ------------------------------------------------------------------

    @app.get("/publications", response_class=HTMLResponse)
    async def publications_list(request: Request):
        repo = _repo(request)
        try:
            pubs = repo.list_publications()
            pubs.sort(key=lambda p: p.name.lower())

            # Collect unique categories across all publications for the filter.
            seen: dict[int, str] = {}
            for p in pubs:
                for cat in p.categories:
                    seen.setdefault(cat.category_id, cat.category_name)
            categories = sorted(seen.items(), key=lambda kv: kv[1].lower())

            csrf = generate_csrf_token(request)
            return _templates(request).TemplateResponse(
                request,
                "publications.html",
                {
                    "publications": pubs,
                    "categories": categories,
                    "csrf_token": csrf,
                },
            )
        finally:
            repo.session.close()

    @app.get("/search", response_class=HTMLResponse)
    async def search_issues(request: Request):
        """Search issues across every publication (TASK-1364).

        Unlike /publications/{code} (search within one publication) and
        /library (search filenames on disk), this searches the issues
        table itself, across all 94+ publications - the thing missing
        when you remember a title's year but not which publication it
        belongs to. The DB query is bounded (see
        ``DownloadRepository.search_issues``); nothing here loads the
        full issues table into Python or ships it to the browser to
        filter client-side.
        """
        q = (request.query_params.get("q") or "").strip()
        status = (request.query_params.get("status") or "").strip()
        downloaded = (request.query_params.get("downloaded") or "").strip()
        valid_statuses = {s.value for s in IssueStatus}
        if status not in valid_statuses:
            status = ""
        if downloaded not in ("yes", "no"):
            downloaded = ""
        searched = bool(q or status or downloaded)

        repo = _repo(request)
        try:
            results: list = []
            has_more = False
            if searched:
                results, has_more = repo.search_issues(
                    query=q, status=status or None, downloaded=downloaded or None
                )
            return _templates(request).TemplateResponse(
                request,
                "search.html",
                {
                    "q": q,
                    "status": status,
                    "downloaded": downloaded,
                    "results": results,
                    "has_more": has_more,
                    "searched": searched,
                    "result_limit": DownloadRepository.SEARCH_ISSUE_LIMIT,
                },
            )
        finally:
            repo.session.close()

    @app.post("/publications/{code}/watch", response_class=HTMLResponse)
    async def watch_publication(
        request: Request, code: str, folder_name: str = Form("")
    ):
        """Start watching - bevaka framåt only (TASK-1361).

        Watching queues nothing itself: it just flips the flag so poll
        starts auto-queuing issues discovered from now on. The back
        catalogue is a deliberate, separate action - the "Queue missing
        issues" button on the publication's detail page - so ticking
        Watch can never accidentally trigger a multi-hundred-gigabyte
        download.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        folder_error = None
        folder_required = False
        folder_name_response_value = folder_name
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            publication = repo.get_publication(code)
            if publication is None:
                return HTMLResponse("Publication not found", status_code=404)
            conflict = repo.publication_folder_conflict(code)
            if folder_name or conflict is not None:
                if not folder_name:
                    folder_required = True
                else:
                    try:
                        repo.set_publication_folder_name(
                            code, folder_name, request.app.state.output_root
                        )
                    except PublicationFolderConflict:
                        folder_error = _FOLDER_NAME_CONFLICT
                        folder_required = True
                    except PublicationFolderMoveError:
                        folder_error = _FOLDER_MOVE_FAILED
                        folder_required = True
                    except PublicationFolderError:
                        folder_error = _INVALID_FOLDER_NAME
                        folder_required = True
            if not folder_required:
                repo.set_watched(code, True)
            folder_name_response_value = folder_name or publication.folder_name or ""
        return await _publication_row(
            request,
            code,
            folder_name_required=folder_required,
            folder_error=folder_error,
            folder_name_value=folder_name_response_value,
        )

    @app.post("/publications/{code}/unwatch", response_class=HTMLResponse)
    async def unwatch_publication(request: Request, code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            DownloadRepository(session).set_watched(code, False)
        return await _publication_row(request, code)

    @app.post("/publications/{code}/poll-interval", response_class=HTMLResponse)
    async def set_poll_interval(
        request: Request, code: str, poll_interval_minutes: str = Form("")
    ):
        """Set (or clear) this publication's own poll interval (TASK-1291).

        A blank field clears the override and reverts to the global
        default - queued/backfilled on every poll tick, same as a
        publication that never had one.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        raw = poll_interval_minutes.strip()
        minutes: int | None = None
        if raw:
            try:
                minutes = int(raw)
            except ValueError:
                return HTMLResponse(
                    "Poll interval must be a whole number", status_code=400
                )
            if minutes <= 0:
                minutes = None
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            if not repo.set_publication_poll_interval(code, minutes):
                return HTMLResponse("Publication not found", status_code=404)
        return RedirectResponse(f"/publications/{code}", status_code=303)

    @app.get("/publications/{code}/cover")
    async def serve_publication_cover(request: Request, code: str):
        """Serve the locally cached cover for *code* (TASK-1345).

        Never hits pagesuite/Flipp directly - the cache is populated by
        the poll tick (``scheduler._cache_covers``), not on request, so
        this stays cheap even when a page renders a hundred rows.
        """
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
        finally:
            repo.session.close()
        if pub is None:
            return HTMLResponse("Publication not found", status_code=404)
        resolved = _safe_cover_file(pub.cover_cache_path)
        if resolved is None:
            return HTMLResponse("Cover not cached", status_code=404)
        return FileResponse(resolved)

    @app.get("/publications/{code}/cover/large")
    async def serve_publication_cover_large(request: Request, code: str):
        """Serve a larger cover for the lightbox (TASK-1396), fetched on demand.

        Unlike ``serve_publication_cover`` above, this *does* hit
        pagesuite directly - but only when a user actually opens the
        lightbox, not on every list render. ``cover_url`` already is the
        600m variant (``scheduler.cache_covers`` downgrades it to 300m
        for the thumbnail cache), so no size juggling is needed here.
        Cached to disk by filename convention only - no DB column tracks
        it, so there is no write transaction to hold open across the
        network call.
        """
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
        finally:
            repo.session.close()
        if pub is None or not pub.cover_url:
            return HTMLResponse("Cover not available", status_code=404)
        cache_root = default_cover_cache_root()
        stem = f"pub-{code}-large"
        cached = find_cached_cover(cache_root, stem)
        if cached is None:
            filename = fetch_and_cache_cover(pub.cover_url, cache_root, stem)
            if filename is None:
                return HTMLResponse("Cover not available", status_code=502)
            cached = cache_root / filename
        return FileResponse(cached)

    async def _publication_row(
        request: Request,
        code: str,
        *,
        folder_name_required: bool = False,
        folder_error: str | None = None,
        folder_name_value: str = "",
    ) -> HTMLResponse:
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            return _templates(request).TemplateResponse(
                request,
                "publication_row.html",
                {
                    "publication": pub,
                    "csrf_token": generate_csrf_token(request),
                    "folder_name_required": folder_name_required,
                    "folder_error": folder_error,
                    "folder_name_value": folder_name_value,
                },
            )
        finally:
            repo.session.close()

    # ------------------------------------------------------------------
    # OPDS catalog feeds (TASK-1294) – Atom 1.2 and JSON 2.0, same
    # catalog logic shared via flipp_dl.web.opds. Mounted under /api/
    # so AuthMiddleware answers an unauthenticated request with a JSON
    # 401 instead of an HTML redirect an OPDS client can't follow (see
    # opds.py's module docstring / the task report for the auth
    # trade-off this implies).
    # ------------------------------------------------------------------

    @app.get("/api/opds")
    async def opds_root_atom(request: Request):
        repo = _repo(request)
        try:
            feed = opds.build_root_feed(repo, request, json_format=False)
        finally:
            repo.session.close()
        return Response(
            content=opds.render_atom(feed), media_type=opds.ATOM_CONTENT_TYPE
        )

    @app.get("/api/opds2")
    async def opds_root_json(request: Request):
        repo = _repo(request)
        try:
            feed = opds.build_root_feed(repo, request, json_format=True)
        finally:
            repo.session.close()
        return JSONResponse(opds.render_json(feed), media_type=opds.JSON_CONTENT_TYPE)

    @app.get("/api/opds/{code}")
    async def opds_publication_atom(request: Request, code: str):
        repo = _repo(request)
        try:
            feed = opds.build_publication_feed(
                repo, request, request.app.state.output_root, code, json_format=False
            )
        finally:
            repo.session.close()
        if feed is None:
            return PlainTextResponse("Publication not found", status_code=404)
        return Response(
            content=opds.render_atom(feed),
            media_type=opds.ATOM_ACQUISITION_CONTENT_TYPE,
        )

    @app.get("/api/opds2/{code}")
    async def opds_publication_json(request: Request, code: str):
        repo = _repo(request)
        try:
            feed = opds.build_publication_feed(
                repo, request, request.app.state.output_root, code, json_format=True
            )
        finally:
            repo.session.close()
        if feed is None:
            return JSONResponse(
                {"error": "publication not found", "custom_code": code}, status_code=404
            )
        return JSONResponse(opds.render_json(feed), media_type=opds.JSON_CONTENT_TYPE)

    # ------------------------------------------------------------------
    # Publication detail – per-issue list and manual downloads
    # ------------------------------------------------------------------

    def _publication_detail_response(
        request: Request,
        repo: DownloadRepository,
        pub,
        *,
        destination_error: str | None = None,
        status_code: int = 200,
    ):
        # Newest issues first - issue_date is a YYYY-MM-DD string so
        # lexicographic sort matches chronological order.
        issues = sorted(pub.issues, key=lambda i: i.issue_date or "", reverse=True)
        _annotate_file_exists(issues, request.app.state.output_root)
        return _templates(request).TemplateResponse(
            request,
            "publication_detail.html",
            {
                "publication": pub,
                "issues": issues,
                "delisted_issue_count": sum(1 for issue in issues if issue.delisted_at),
                "komga": _komga_status(repo, pub),
                "csrf_token": generate_csrf_token(request),
                "queue_estimate": repo.estimate_missing_download_size(pub.id),
                "queue_warn_threshold_bytes": repo.queue_warn_threshold_bytes(),
                "destination_error": destination_error,
            },
            status_code=status_code,
        )

    @app.get("/publications/{code}", response_class=HTMLResponse)
    async def publication_detail(request: Request, code: str):
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            return _publication_detail_response(request, repo, pub)
        finally:
            repo.session.close()

    @app.post("/publications/{code}/destination", response_class=HTMLResponse)
    async def set_publication_destination(
        request: Request, code: str, destination: str = Form("primary")
    ):
        """Set the publication output root without moving existing files."""
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        try:
            with get_session(request.app.state.session_factory) as session:
                changed = DownloadRepository(session).set_publication_destination(
                    code, destination
                )
                if not changed:
                    return HTMLResponse("Publication not found", status_code=404)
        except PublicationDestinationError:
            lang = i18n.get_language(request)
            message = i18n.translate(lang, _DESTINATION_CHANGE_BLOCKED)
            repo = _repo(request)
            try:
                publication = repo.get_publication(code)
                if publication is None:
                    return HTMLResponse("Publication not found", status_code=404)
                return _publication_detail_response(
                    request,
                    repo,
                    publication,
                    destination_error=message,
                    status_code=400,
                )
            finally:
                repo.session.close()
        return RedirectResponse(f"/publications/{code}", status_code=303)

    @app.post("/publications/{code}/notify", response_class=HTMLResponse)
    async def set_publication_notify(
        request: Request, code: str, notify_enabled: str = Form("off")
    ):
        """Turn notifications on or off for one publication (TASK-1447).

        Independent of watching: watching decides what is downloaded,
        this decides what is announced. A channel still has to be
        configured under /settings for anything to be sent at all.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            found = DownloadRepository(session).set_publication_notify(
                code, notify_enabled == "on"
            )
        if not found:
            return HTMLResponse("Publication not found", status_code=404)
        return RedirectResponse(f"/publications/{code}", status_code=303)

    @app.post("/publications/{code}/folder-name", response_class=HTMLResponse)
    async def set_publication_folder_name(
        request: Request, code: str, folder_name: str = Form("")
    ):
        """Set or clear the user-selected folder name, moving owned files."""
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        lang = i18n.get_language(request)
        try:
            with get_session(request.app.state.session_factory) as session:
                changed = DownloadRepository(session).set_publication_folder_name(
                    code, folder_name, request.app.state.output_root
                )
                if not changed:
                    return HTMLResponse("Publication not found", status_code=404)
        except PublicationFolderConflict:
            message = i18n.translate(lang, _FOLDER_NAME_CONFLICT)
            return HTMLResponse(message, status_code=400)
        except PublicationFolderMoveError:
            return HTMLResponse(
                i18n.translate(lang, _FOLDER_MOVE_FAILED), status_code=400
            )
        except PublicationFolderError:
            return HTMLResponse(
                i18n.translate(lang, _INVALID_FOLDER_NAME), status_code=400
            )
        return RedirectResponse(f"/publications/{code}", status_code=303)

    @app.post("/publications/{code}/queue-missing", response_class=HTMLResponse)
    async def queue_missing(request: Request, code: str):
        """Queue every issue of *code* that isn't downloaded yet.

        Separate from watching, so catching up on a publication doesn't
        mean unwatching and re-watching it - which would leave a gap
        where a poll could miss new issues.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            queued = repo.queue_missing_issues(pub.id)
        logger.info("Queue-missing %s: queued %d issue(s)", code, queued)
        if queued:
            label = "issue" if queued == 1 else "issues"
            body = f"Queued {queued} {label}"
        else:
            body = "Nothing to queue"
        # Reload so every affected row picks up its new status.
        return HTMLResponse(
            f'<span class="dim" hx-get="/publications/{code}" '
            f'hx-trigger="load delay:1200ms" hx-target="body" '
            f'hx-push-url="true">{body} – refreshing…</span>'
        )

    @app.get(
        "/publications/{code}/issues/{issue_code}/row",
        response_class=HTMLResponse,
    )
    async def issue_row_partial(request: Request, code: str, issue_code: str):
        """Return the single issue row – used by HTMX polling to refresh
        the status badge and progress counter while a download is live.
        """
        return await _issue_row(request, code, issue_code)

    @app.post(
        "/publications/{code}/issues/{issue_code}/download",
        response_class=HTMLResponse,
    )
    async def download_issue_manual(request: Request, code: str, issue_code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
            # Don't create duplicate jobs while one is already in flight.
            if issue.status not in (IssueStatus.QUEUED, IssueStatus.DOWNLOADING):
                repo.mark_issue_queued(issue.id)
                repo.create_job("download", {"issue_id": issue.id})
        return await _issue_row(request, code, issue_code)

    @app.post(
        "/publications/{code}/issues/{issue_code}/cancel",
        response_class=HTMLResponse,
    )
    async def cancel_issue_download(request: Request, code: str, issue_code: str):
        """Release an issue stuck in queued/downloading.

        Any job still pointing at it is finished as cancelled so the
        scheduler cannot pick it up afterwards.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
            repo.cancel_issue(issue.id)
        return await _issue_row(request, code, issue_code)

    async def _issue_row(request: Request, code: str, issue_code: str) -> HTMLResponse:
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
            _annotate_file_exists([issue], request.app.state.output_root)
            return _templates(request).TemplateResponse(
                request,
                "issue_row.html",
                {
                    "publication": pub,
                    "issue": issue,
                    "csrf_token": generate_csrf_token(request),
                },
            )
        finally:
            repo.session.close()

    @app.post(
        "/publications/{code}/issues/{issue_code}/delete",
        response_class=HTMLResponse,
    )
    async def delete_issue_file(request: Request, code: str, issue_code: str):
        """Remove the downloaded PDF and reset the issue to NEW.

        The file is only unlinked if it sits inside ``output_root`` – the
        same traversal guard used when serving files – so a malicious or
        stale ``file_path`` can't delete arbitrary files.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
            if issue.status in (IssueStatus.QUEUED, IssueStatus.DOWNLOADING):
                return HTMLResponse(
                    "Cannot delete while a download is in progress",
                    status_code=409,
                )

            resolved = _safe_output_file(request.app.state.output_root, issue.file_path)
            if resolved is not None:
                try:
                    resolved.unlink()
                except OSError:
                    # Leave DB state alone if we can't remove the file – the
                    # user will see the file still listed and can retry.
                    return HTMLResponse(
                        "Failed to delete file on disk", status_code=500
                    )

            repo.reset_issue(issue.id)

        return await _issue_row(request, code, issue_code)

    @app.get("/publications/{code}/issues/{issue_code}/cover")
    async def serve_issue_cover(request: Request, code: str, issue_code: str):
        """Serve the locally cached cover thumbnail for one issue (TASK-1345)."""
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
        finally:
            repo.session.close()
        if issue is None:
            return HTMLResponse("Issue not found", status_code=404)
        resolved = _safe_cover_file(issue.cover_cache_path)
        if resolved is None:
            return HTMLResponse("Cover not cached", status_code=404)
        return FileResponse(resolved)

    @app.get("/publications/{code}/issues/{issue_code}/cover/large")
    async def serve_issue_cover_large(request: Request, code: str, issue_code: str):
        """Serve a larger issue cover for the lightbox (TASK-1396), on demand.

        The thumbnail cache uses ``w=100``; ``w=600`` is the largest
        width pagesuite serves before returning 403 (measured directly -
        ``w=1200`` is forbidden). Fetched only when a user opens the
        lightbox, then cached to disk by filename convention - see
        ``serve_publication_cover_large`` for why that avoids a DB write
        transaction around the network call.
        """
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
        finally:
            repo.session.close()
        if issue is None:
            return HTMLResponse("Issue not found", status_code=404)
        cache_root = default_cover_cache_root()
        stem = f"issue-{issue_code}-large"
        cached = find_cached_cover(cache_root, stem)
        if cached is None:
            url = (
                "https://edition.pagesuite-professional.co.uk/get_image.aspx"
                f"?w=600&eid={issue_code}"
            )
            filename = fetch_and_cache_cover(url, cache_root, stem)
            if filename is None:
                return HTMLResponse("Cover not available", status_code=502)
            cached = cache_root / filename
        return FileResponse(cached)

    @app.get("/publications/{code}/issues/{issue_code}/file")
    async def serve_issue_file(request: Request, code: str, issue_code: str):
        """Stream the merged issue PDF back to the browser for inline view."""
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None or not issue.file_path:
                return HTMLResponse("File not available", status_code=404)
            resolved = _safe_output_file(request.app.state.output_root, issue.file_path)
            if resolved is None:
                return HTMLResponse("File not available", status_code=404)
            return FileResponse(
                resolved,
                media_type="application/pdf",
                filename=resolved.name,
                content_disposition_type="inline",
            )
        finally:
            repo.session.close()

    @app.get("/publications/{code}/issues/{issue_code}/preview")
    async def preview_issue(request: Request, code: str, issue_code: str):
        """Fetch a few leading pages of an issue and stream them inline.

        Deliberately does not go through the job queue or
        ``download_issue()`` - the issue's DB status is left completely
        alone, so a preview never shows up in /jobs or as a queued /
        downloading row (TASK-1344).
        """
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
            domain_pub = DomainPublication(custom_code=pub.custom_code, name=pub.name)
            domain_issue = DomainIssue(
                custom_code=issue.custom_code,
                issue_name=issue.issue_name,
                issue_date=issue.issue_date,
            )
        finally:
            repo.session.close()

        token = resolve_current_token(request.app.state.session_factory)
        if not token:
            return JSONResponse({"error": "No token configured"}, status_code=400)

        client = FlippClient(token)
        # No repository wired in: preview_issue() never reports status
        # through one, but omitting it entirely rules out a future
        # change accidentally reaching for self.repository here.
        downloader = IssueDownloader(client, request.app.state.output_root)
        try:
            preview_path = downloader.preview_issue(domain_pub, domain_issue)
        except FlippError as exc:
            return HTMLResponse(str(exc), status_code=502)

        return FileResponse(
            preview_path,
            media_type="application/pdf",
            filename=preview_path.name,
            content_disposition_type="inline",
        )

    # ------------------------------------------------------------------
    # Library – browse everything currently on disk under output_root
    # ------------------------------------------------------------------

    @app.get("/library", response_class=HTMLResponse)
    async def library_index(request: Request):
        output_root: Path = request.app.state.output_root
        groups: list[tuple[str, list[dict]]] = []
        total_bytes = 0
        total_files = 0
        if output_root.is_dir():
            buckets: dict[str, list[dict]] = {}
            for pdf in output_root.rglob("*.pdf"):
                if not pdf.is_file():
                    continue
                try:
                    rel = pdf.relative_to(output_root)
                except ValueError:
                    continue
                try:
                    stat = pdf.stat()
                except OSError:
                    continue
                folder = str(rel.parent) if str(rel.parent) != "." else ""
                buckets.setdefault(folder, []).append(
                    {
                        "name": pdf.name,
                        "rel_path": str(rel).replace(os.sep, "/"),
                        "size": stat.st_size,
                        # Timezone-aware UTC: the localtime filter would otherwise
                        # read a naive local time as UTC and shift it twice.
                        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    }
                )
                total_bytes += stat.st_size
                total_files += 1
            for folder in sorted(buckets.keys(), key=str.lower):
                buckets[folder].sort(key=lambda f: f["name"].lower())
                groups.append((folder, buckets[folder]))
        return _templates(request).TemplateResponse(
            request,
            "library.html",
            {
                "groups": groups,
                "total_files": total_files,
                "total_bytes": total_bytes,
                "output_root": str(output_root),
                # The reconcile-with-disk action moved here from the
                # settings page (TASK-1398) and posts with CSRF.
                "csrf_token": generate_csrf_token(request),
            },
        )

    @app.get("/library/file/{rel_path:path}")
    async def serve_library_file(request: Request, rel_path: str):
        resolved = _safe_output_file(request.app.state.output_root, rel_path)
        if resolved is None:
            return HTMLResponse("File not available", status_code=404)
        return FileResponse(
            resolved,
            media_type="application/pdf",
            filename=resolved.name,
            content_disposition_type="inline",
        )

    @app.post("/publications/{code}/poll", response_class=HTMLResponse)
    async def poll_single(request: Request, code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        token = resolve_current_token(request.app.state.session_factory)
        if not token:
            return JSONResponse({"error": "No token configured"}, status_code=400)
        client = FlippClient(token)
        workers = int(os.environ.get("FLIPP_WORKERS", "4"))
        poll_publications(
            client,
            request.app.state.session_factory,
            request.app.state.output_root,
            workers,
        )
        return HTMLResponse("<span>Polled ✓</span>")

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    # These end up in URLs and in the template. JobStatus is a StrEnum, so
    # a member renders as its own value ("queued") - no .value needed.
    _JOB_STATUSES = (
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.DONE,
        JobStatus.ERROR,
        JobStatus.RETRY_PENDING,
    )

    def _job_issue_id(job) -> int | None:
        """Return the issue id a download job points at, if any.

        A job whose payload is malformed still has to render - the row
        just shows no target.
        """
        try:
            payload = json.loads(job.payload or "{}")
        except (TypeError, ValueError):
            return None
        issue_id = payload.get("issue_id")
        try:
            return int(issue_id) if issue_id is not None else None
        except (TypeError, ValueError):
            return None

    def _job_targets(repo: DownloadRepository, jobs: list) -> dict[int, object]:
        """Map ``job.id -> DbIssue`` for the listed jobs, in one query."""
        wanted = {job.id: _job_issue_id(job) for job in jobs}
        issues = repo.get_issues_by_ids([i for i in wanted.values() if i is not None])
        return {
            job_id: issues[issue_id]
            for job_id, issue_id in wanted.items()
            if issue_id is not None and issue_id in issues
        }

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_list(request: Request, status: str = ""):
        """List jobs, optionally narrowed to a single status.

        An unknown *status* is treated as no filter rather than an
        error - the value comes from a bookmarkable URL.
        """
        selected = status if status in _JOB_STATUSES else ""
        repo = _repo(request)
        try:
            jobs = repo.list_jobs(limit=100, status=selected or None)
            counts = repo.count_jobs_by_status()
            targets = _job_targets(repo, jobs)
            # Lets the template link a finished issue straight to its PDF.
            _annotate_file_exists(targets.values(), request.app.state.output_root)
            return _templates(request).TemplateResponse(
                request,
                "jobs.html",
                {
                    "jobs": jobs,
                    "targets": targets,
                    "counts": counts,
                    "total_jobs": sum(counts.values()),
                    "selected_status": selected,
                    "statuses": _JOB_STATUSES,
                },
            )
        finally:
            repo.session.close()

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int):
        repo = _repo(request)
        try:
            job = repo.get_job(job_id)
            if job is None:
                return HTMLResponse("Job not found", status_code=404)
            issue_id = _job_issue_id(job)
            issue = repo.get_issue(issue_id) if issue_id is not None else None
            if issue is not None:
                _annotate_file_exists([issue], request.app.state.output_root)
            try:
                payload = json.dumps(json.loads(job.payload or "{}"), indent=2)
            except (TypeError, ValueError):
                payload = job.payload or ""
            return _templates(request).TemplateResponse(
                request,
                "job_detail.html",
                {"job": job, "issue": issue, "payload": payload},
            )
        finally:
            repo.session.close()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _token_settings(repo: DownloadRepository) -> dict:
        """Token status for the settings template - never the value itself.

        ``token_saved`` means a token was explicitly stored via this form
        and takes precedence at the next poll. ``token_env_fallback`` means
        no such token exists yet, but ``FLIPP_TOKEN``/the token file will
        be used in the meantime.
        """
        saved = bool(repo.get_setting("flipp_token", "").strip())
        return {
            "token_saved": saved,
            "token_env_fallback": (not saved) and bool(load_token()),
        }

    def _komga_view_settings(repo: DownloadRepository) -> dict:
        """Komga settings for the template - secrets never rendered back.

        ``komga_password``/``komga_api_key`` only ever surface as a
        boolean ("is one saved") - the same pattern as the Flipp token:
        an empty field with a bullet placeholder, saved only when the
        field is actually filled in.
        """
        return {
            "komga_enabled": repo.get_setting("komga_enabled", "").strip().lower()
            == "true",
            "komga_url": repo.get_setting("komga_url", ""),
            "komga_username": repo.get_setting("komga_username", ""),
            "komga_library_id": repo.get_setting("komga_library_id", ""),
            "komga_password_saved": bool(
                repo.get_setting("komga_password", "").strip()
            ),
            "komga_api_key_saved": bool(repo.get_setting("komga_api_key", "").strip()),
        }

    def _notify_view_settings(repo: DownloadRepository) -> dict:
        """Notification settings for the template (TASK-1293).

        ``notify_ntfy_token``/``notify_webhook_url`` are secrets - same
        "boolean saved-state only" pattern as the Komga password/API key
        above, never the value itself.
        """
        return {
            "notify_ntfy_enabled": repo.get_setting("notify_ntfy_enabled", "")
            .strip()
            .lower()
            == "true",
            "notify_ntfy_url": repo.get_setting("notify_ntfy_url", ""),
            "notify_ntfy_topic": repo.get_setting("notify_ntfy_topic", ""),
            "notify_ntfy_token_saved": bool(
                repo.get_setting("notify_ntfy_token", "").strip()
            ),
            "notify_webhook_enabled": repo.get_setting("notify_webhook_enabled", "")
            .strip()
            .lower()
            == "true",
            "notify_webhook_url_saved": bool(
                repo.get_setting("notify_webhook_url", "").strip()
            ),
        }

    def _format_gb(threshold_bytes: int) -> str:
        """Render *threshold_bytes* as a trimmed GB string for the form field."""
        gb = threshold_bytes / (1024**3)
        formatted = f"{gb:.2f}".rstrip("0").rstrip(".")
        return formatted or "0"

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        repo = _repo(request)
        try:
            settings = {
                "poll_interval": repo.get_setting("poll_interval", "360"),
                "workers": repo.get_setting("workers", "4"),
                "secondary_output_root": repo.get_setting("secondary_output_root", ""),
                "queue_warn_threshold_gb": _format_gb(
                    repo.queue_warn_threshold_bytes()
                ),
                **_token_settings(repo),
                **_komga_view_settings(repo),
                **_notify_view_settings(repo),
            }
            csrf = generate_csrf_token(request)
            return _templates(request).TemplateResponse(
                request, "settings.html", {"settings": settings, "csrf_token": csrf}
            )
        finally:
            repo.session.close()

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(
        request: Request,
        poll_interval: int = Form(360),
        workers: int = Form(4),
        secondary_output_root: str = Form(""),
        queue_warn_threshold_gb: str = Form(""),
        flipp_token: str = Form(""),
        komga_enabled: str | None = Form(None),
        komga_url: str = Form(""),
        komga_username: str = Form(""),
        komga_password: str = Form(""),
        komga_api_key: str = Form(""),
        komga_library_id: str = Form(""),
        notify_ntfy_enabled: str | None = Form(None),
        notify_ntfy_url: str = Form(""),
        notify_ntfy_topic: str = Form(""),
        notify_ntfy_token: str = Form(""),
        notify_webhook_enabled: str | None = Form(None),
        notify_webhook_url: str = Form(""),
    ):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        token_value = flipp_token.strip()

        # A blank field resets the threshold to the FLIPP_QUEUE_WARN_THRESHOLD_BYTES
        # env var / 5 GiB default (same "blank = clear the override" rule as the
        # per-publication poll interval). Anything else must parse as a positive
        # number - an invalid value must not silently save a broken threshold.
        queue_warn_threshold_raw = queue_warn_threshold_gb.strip()
        queue_warn_threshold_bytes_value: int | None = None
        if queue_warn_threshold_raw:
            try:
                gb_value = float(queue_warn_threshold_raw)
            except ValueError:
                return HTMLResponse(
                    "Backfill confirm threshold must be a number", status_code=400
                )
            if gb_value <= 0:
                return HTMLResponse(
                    "Backfill confirm threshold must be greater than zero",
                    status_code=400,
                )
            queue_warn_threshold_bytes_value = round(gb_value * 1024**3)

        # Set when a field was rejected: the page re-renders with the error
        # and without the "saved" flash, keeping what was typed on screen.
        settings_error: str | None = None

        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_setting("poll_interval", str(poll_interval))
            repo.set_setting("workers", str(workers))
            secondary_output_value = secondary_output_root.strip()
            secondary_resolved = None
            if secondary_output_value:
                try:
                    secondary_resolved = (
                        Path(secondary_output_value).expanduser().resolve()
                    )
                except (OSError, RuntimeError):
                    secondary_resolved = None
                if secondary_resolved is None or not secondary_resolved.is_dir():
                    settings_error = i18n.translate(
                        i18n.get_language(request), _SECONDARY_ROOT_MISSING
                    )
                    secondary_resolved = None
            if settings_error is None:
                repo.set_setting(
                    "secondary_output_root",
                    str(secondary_resolved) if secondary_resolved else "",
                )
            repo.set_setting(
                "queue_warn_threshold_bytes",
                (
                    str(queue_warn_threshold_bytes_value)
                    if queue_warn_threshold_bytes_value is not None
                    else ""
                ),
            )
            # Empty input leaves a previously saved token untouched - the
            # form field is never pre-filled with the real value, so a
            # blank submit must not be read as "clear the token".
            if token_value:
                repo.set_setting("flipp_token", token_value)

            repo.set_setting("komga_enabled", "true" if komga_enabled else "false")
            repo.set_setting("komga_url", komga_url.strip())
            repo.set_setting("komga_username", komga_username.strip())
            # Same "blank = keep unchanged" rule as the Flipp token above.
            if komga_password.strip():
                repo.set_setting("komga_password", komga_password.strip())
            if komga_api_key.strip():
                repo.set_setting("komga_api_key", komga_api_key.strip())
            repo.set_setting("komga_library_id", komga_library_id.strip())

            repo.set_setting(
                "notify_ntfy_enabled", "true" if notify_ntfy_enabled else "false"
            )
            repo.set_setting("notify_ntfy_url", notify_ntfy_url.strip())
            repo.set_setting("notify_ntfy_topic", notify_ntfy_topic.strip())
            if notify_ntfy_token.strip():
                repo.set_setting("notify_ntfy_token", notify_ntfy_token.strip())
            repo.set_setting(
                "notify_webhook_enabled", "true" if notify_webhook_enabled else "false"
            )
            if notify_webhook_url.strip():
                repo.set_setting("notify_webhook_url", notify_webhook_url.strip())

            queue_warn_threshold_gb_display = _format_gb(
                repo.queue_warn_threshold_bytes()
            )
            secondary_output_root_display = (
                secondary_output_value
                if settings_error
                else repo.get_setting("secondary_output_root", "")
            )
            token_settings = _token_settings(repo)
            komga_settings = _komga_view_settings(repo)
            notify_settings = _notify_view_settings(repo)

        csrf = generate_csrf_token(request)
        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            {
                "settings": {
                    "poll_interval": str(poll_interval),
                    "workers": str(workers),
                    "secondary_output_root": secondary_output_root_display,
                    "queue_warn_threshold_gb": queue_warn_threshold_gb_display,
                    **token_settings,
                    **komga_settings,
                    **notify_settings,
                },
                "csrf_token": csrf,
                "saved": settings_error is None,
                "settings_error": settings_error,
            },
        )

    @app.post("/settings/komga/test", response_class=HTMLResponse)
    async def komga_test_connection(
        request: Request,
        komga_url: str = Form(""),
        komga_username: str = Form(""),
        komga_password: str = Form(""),
        komga_api_key: str = Form(""),
    ):
        """List libraries from a not-yet-saved (or partially saved) config.

        A blank password/API-key field falls back to whatever is already
        saved - the field is never pre-filled with the real secret (same
        rule as the save form), so testing right after opening the page
        must still be able to use the saved credential.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        url = komga_url.strip()
        repo = _repo(request)
        try:
            password = komga_password.strip() or repo.get_setting("komga_password", "")
            api_key = komga_api_key.strip() or repo.get_setting("komga_api_key", "")
            selected = repo.get_setting("komga_library_id", "")
        finally:
            repo.session.close()

        if not url:
            return _templates(request).TemplateResponse(
                request,
                "komga_library_select.html",
                {
                    "error": "Enter a Komga URL first.",
                    "libraries": None,
                    "selected": selected,
                },
            )

        client = KomgaClient(
            url, username=komga_username.strip(), password=password, api_key=api_key
        )
        try:
            libraries = client.list_libraries()
        except KomgaError as exc:
            logger.warning("Komga test connection to %s failed: %s", url, exc)
            lang = i18n.get_language(request)
            message = i18n.translate(
                lang,
                _KOMGA_TEST_CONNECTION_MESSAGES.get(
                    exc.reason, _KOMGA_TEST_CONNECTION_MESSAGES["other"]
                ),
            )
            return _templates(request).TemplateResponse(
                request,
                "komga_library_select.html",
                {"error": message, "libraries": None, "selected": selected},
            )

        return _templates(request).TemplateResponse(
            request,
            "komga_library_select.html",
            {"error": None, "libraries": libraries, "selected": selected},
        )

    @app.post("/settings/komga/backfill", response_class=HTMLResponse)
    async def komga_backfill(request: Request):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        repo = _repo(request)
        try:
            # The same three-part guard the download path uses before it
            # queues a sync (TASK-1326): an incomplete configuration must
            # not produce jobs. Without it, jobs queued here would sit
            # forever - run_komga_sync_queue returns without draining
            # anything when Komga is off.
            settings = resolve_komga_settings_from_repo(repo)
            library_id = settings["library_id"]
            if not (settings["enabled"] and settings["url"] and library_id):
                return _templates(request).TemplateResponse(
                    request,
                    "komga_backfill_result.html",
                    {"not_configured": True, "queued": 0, "remaining": 0},
                )

            queued, remaining = repo.queue_komga_backfill(
                library_id, KOMGA_BACKFILL_LIMIT
            )
            repo.session.commit()
            return _templates(request).TemplateResponse(
                request,
                "komga_backfill_result.html",
                {
                    "library_missing": False,
                    "queued": queued,
                    "remaining": remaining,
                },
            )
        finally:
            repo.session.close()

    @app.post("/settings/notify/test", response_class=HTMLResponse)
    async def notify_test(
        request: Request,
        notify_ntfy_url: str = Form(""),
        notify_ntfy_topic: str = Form(""),
        notify_ntfy_token: str = Form(""),
    ):
        """Send one test notification through ntfy.

        Uses whatever is in the form right now, falling back to what is
        saved for any blank field - the same rule the save form and the
        Komga test button follow, so testing before saving works and a
        blank token field still means "keep the saved one".
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        lang = i18n.get_language(request)
        repo = _repo(request)
        try:
            url = notify_ntfy_url.strip() or repo.get_setting("notify_ntfy_url", "")
            topic = notify_ntfy_topic.strip() or repo.get_setting(
                "notify_ntfy_topic", ""
            )
            token = notify_ntfy_token.strip() or repo.get_setting(
                "notify_ntfy_token", ""
            )
        finally:
            repo.session.close()

        if not url.strip() or not topic.strip():
            return _templates(request).TemplateResponse(
                request,
                "notify_test_result.html",
                {"error": i18n.translate(lang, _NTFY_TEST_NO_TOPIC), "message": None},
            )

        channel = NtfyChannel(url.strip(), topic.strip(), token.strip())
        try:
            channel.send(
                "Flipp-DL",
                i18n.translate(
                    lang,
                    _("This is a test notification from flipp-dl. Nothing is wrong."),
                ),
            )
        except NotifyError as exc:
            logger.warning("ntfy test failed: %s", exc)
            return _templates(request).TemplateResponse(
                request,
                "notify_test_result.html",
                {
                    "error": i18n.translate(lang, _NTFY_TEST_FAILED),
                    "detail": str(exc),
                    "message": None,
                },
            )

        return _templates(request).TemplateResponse(
            request,
            "notify_test_result.html",
            {"error": None, "message": i18n.translate(lang, _NTFY_TEST_SENT)},
        )

    @app.post("/settings/flipp-login", response_class=HTMLResponse)
    async def flipp_login(
        request: Request, email: str = Form(""), password: str = Form("")
    ):
        """Sign in to Flipp and store the token it hands back (TASK-1454).

        The alternative was the console snippet next to the token field,
        which needs devtools and a browser already logged in. The web app
        signs in against ``/api/signin`` with the same payload it later
        refreshes with, so flipp-dl can do the same.

        The credentials are used for this one request and never stored -
        only the token is, exactly as if it had been pasted in by hand.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        lang = i18n.get_language(request)
        if not email.strip() or not password:
            return _templates(request).TemplateResponse(
                request,
                "flipp_login_result.html",
                {"error": i18n.translate(lang, _LOGIN_WRONG_CREDENTIALS), "ok": False},
            )
        try:
            token = FlippClient.sign_in(email.strip(), password)
        except FlippError as exc:
            message = (
                _LOGIN_WRONG_CREDENTIALS
                if str(exc) == WRONG_CREDENTIALS
                else _LOGIN_FAILED
            )
            logger.warning("Flipp sign-in failed for %s: %s", email.strip(), exc)
            return _templates(request).TemplateResponse(
                request,
                "flipp_login_result.html",
                {"error": i18n.translate(lang, message), "ok": False},
            )

        with get_session(request.app.state.session_factory) as session:
            DownloadRepository(session).set_setting("flipp_token", token)
        return _templates(request).TemplateResponse(
            request,
            "flipp_login_result.html",
            {"error": None, "ok": True, "message": i18n.translate(lang, _LOGIN_SAVED)},
        )

    @app.post("/settings/poll", response_class=HTMLResponse)
    async def poll_now(request: Request):
        """Run a poll right now instead of waiting for the next tick.

        The scheduler polls every ``FLIPP_POLL_INTERVAL`` minutes (six
        hours by default), and the cover backfill rides along at the end
        of it - so after discovering editions there was no way to see
        their covers without restarting the container (TASK-1465).

        Synchronous, like "Import existing files" next to it: the answer
        is what the poll did, so there is nothing useful to show until it
        has finished. It can take a minute when the cover backlog is
        large; the button shows a spinner meanwhile.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        token = resolve_current_token(request.app.state.session_factory)
        if not token:
            lang = i18n.get_language(request)
            return _templates(request).TemplateResponse(
                request,
                "poll_result.html",
                {"error": i18n.translate(lang, _POLL_NEEDS_TOKEN), "job": None},
            )

        workers = int(os.environ.get("FLIPP_WORKERS", "4"))
        poll_publications(
            FlippClient(token),
            request.app.state.session_factory,
            request.app.state.output_root,
            workers,
        )

        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            jobs = repo.list_jobs(limit=1, job_type="poll")
            job = None
            if jobs:
                job = {
                    "id": jobs[0].id,
                    "status": jobs[0].status,
                    "error": jobs[0].error_message,
                }
            counts = {
                "queued": repo.count_issues_with_status(IssueStatus.QUEUED),
                "missing_covers": repo.count_issues_without_cover(),
            }
        return _templates(request).TemplateResponse(
            request, "poll_result.html", {"error": None, "job": job, "counts": counts}
        )

    @app.post("/library/import-existing", response_class=HTMLResponse)
    async def import_existing(request: Request):
        """Reconcile the DB against what's actually on disk (TASK-1283).

        Read-only on the filesystem: a file that matches a not-yet-done
        issue backfills that issue's status in the DB, it is never
        re-downloaded or moved. Everything the scan can't cleanly
        explain - orphan files, ``done`` issues missing their file, and
        issues sharing one file - is reported instead of silently fixed.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            secondary_output_value = repo.get_setting(
                "secondary_output_root", ""
            ).strip()
            extra_roots = (
                [Path(secondary_output_value)] if secondary_output_value else None
            )
            report = repo.import_existing_files(
                request.app.state.output_root, extra_roots=extra_roots
            )
        return _templates(request).TemplateResponse(
            request, "import_existing_result.html", {"report": report}
        )

    # ------------------------------------------------------------------
    # Debug – manual API poll with raw response viewer
    # ------------------------------------------------------------------

    _SENSITIVE_KEYS = frozenset({"token", "password", "email"})

    def _scrub(data):
        """Recursively mask sensitive keys before sending to the browser."""
        if isinstance(data, dict):
            return {
                k: ("***REDACTED***" if k in _SENSITIVE_KEYS and v else _scrub(v))
                for k, v in data.items()
            }
        if isinstance(data, list):
            return [_scrub(item) for item in data]
        return data

    @app.post("/settings/debug-poll", response_class=HTMLResponse)
    async def debug_poll(request: Request):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        token = resolve_current_token(request.app.state.session_factory)
        if not token:
            return _templates(request).TemplateResponse(
                request,
                "debug_poll_result.html",
                {"error": "No Flipp token configured", "data_json": None},
            )

        try:
            client = FlippClient(token)
            raw = client.fetch_raw_sign_in()
        except FlippError as exc:
            return _templates(request).TemplateResponse(
                request,
                "debug_poll_result.html",
                {"error": str(exc), "data_json": None},
            )
        except Exception as exc:  # noqa: BLE001
            return _templates(request).TemplateResponse(
                request,
                "debug_poll_result.html",
                {"error": f"Unexpected error: {exc}", "data_json": None},
            )

        scrubbed = _scrub(raw)
        # ensure_ascii=False keeps å/ä/ö readable. The replace guards
        # against </script> ever appearing inside a string value and
        # prematurely closing the embedding <script> tag.
        data_json = json.dumps(scrubbed, ensure_ascii=False, indent=2).replace(
            "</", "<\\/"
        )
        num_pubs = len(raw.get("publications", []))
        return _templates(request).TemplateResponse(
            request,
            "debug_poll_result.html",
            {
                "error": None,
                "num_pubs": num_pubs,
                "byte_size": len(data_json),
                "data_json": data_json,
            },
        )
