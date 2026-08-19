"""FastAPI route definitions for flipp-dl web UI."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from .. import storage
from ..api import FlippClient, FlippError
from ..config import load_token
from ..db.models import IssueStatus, JobStatus
from ..db.repository import DownloadRepository, default_cover_cache_root
from ..db.session import get_session
from ..downloader import IssueDownloader
from ..models import Issue as DomainIssue
from ..models import Publication as DomainPublication
from ..scheduler import poll_publications, resolve_current_token
from .auth import auth_enabled, check_csrf_form, generate_csrf_token, verify_password

logger = logging.getLogger(__name__)


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
            "queued_jobs": job_counts[JobStatus.QUEUED.value],
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

    @app.post("/publications/{code}/watch", response_class=HTMLResponse)
    async def watch_publication(request: Request, code: str):
        """Start watching, and queue whatever is not downloaded yet.

        Without this, watching only affects issues discovered by a
        later poll and the back catalogue has to be clicked through by
        hand.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            if not repo.set_watched(code, True):
                return HTMLResponse("Publication not found", status_code=404)
            pub = repo.get_publication(code)
            queued = repo.queue_missing_issues(pub.id)
        if queued:
            logger.info("Watch %s: queued %d missing issue(s)", code, queued)
        return await _publication_row(request, code)

    @app.post("/publications/{code}/unwatch", response_class=HTMLResponse)
    async def unwatch_publication(request: Request, code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            DownloadRepository(session).set_watched(code, False)
        return await _publication_row(request, code)

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

    async def _publication_row(request: Request, code: str) -> HTMLResponse:
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            return _templates(request).TemplateResponse(
                request,
                "publication_row.html",
                {
                    "publication": pub,
                    "csrf_token": generate_csrf_token(request),
                },
            )
        finally:
            repo.session.close()

    # ------------------------------------------------------------------
    # Publication detail – per-issue list and manual downloads
    # ------------------------------------------------------------------

    @app.get("/publications/{code}", response_class=HTMLResponse)
    async def publication_detail(request: Request, code: str):
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            # Newest issues first – issue_date is a YYYY-MM-DD string so
            # lexicographic sort matches chronological order.
            issues = sorted(pub.issues, key=lambda i: i.issue_date or "", reverse=True)
            _annotate_file_exists(issues, request.app.state.output_root)
            return _templates(request).TemplateResponse(
                request,
                "publication_detail.html",
                {
                    "publication": pub,
                    "issues": issues,
                    "csrf_token": generate_csrf_token(request),
                },
            )
        finally:
            repo.session.close()

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
                        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
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

    # Plain strings: these end up in URLs and in the template, where a
    # `str, Enum` member would render as "JobStatus.QUEUED".
    _JOB_STATUSES = (
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
        JobStatus.DONE.value,
        JobStatus.ERROR.value,
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

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        repo = _repo(request)
        try:
            settings = {
                "poll_interval": repo.get_setting("poll_interval", "360"),
                "workers": repo.get_setting("workers", "4"),
                **_token_settings(repo),
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
        flipp_token: str = Form(""),
    ):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        token_value = flipp_token.strip()
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_setting("poll_interval", str(poll_interval))
            repo.set_setting("workers", str(workers))
            # Empty input leaves a previously saved token untouched - the
            # form field is never pre-filled with the real value, so a
            # blank submit must not be read as "clear the token".
            if token_value:
                repo.set_setting("flipp_token", token_value)
            token_settings = _token_settings(repo)

        csrf = generate_csrf_token(request)
        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            {
                "settings": {
                    "poll_interval": str(poll_interval),
                    "workers": str(workers),
                    **token_settings,
                },
                "csrf_token": csrf,
                "saved": True,
            },
        )

    @app.post("/settings/import-existing", response_class=HTMLResponse)
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
            report = repo.import_existing_files(request.app.state.output_root)
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
