"""FastAPI route definitions for flipp-dl web UI."""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..api import FlippClient
from ..config import load_token
from ..db.models import IssueStatus
from ..db.repository import DownloadRepository
from ..db.session import get_session
from ..scheduler import poll_publications
from .auth import auth_enabled, check_csrf_form, generate_csrf_token, verify_password


def _repo(request: Request) -> DownloadRepository:
    session = request.app.state.session_factory()
    return DownloadRepository(session)


def _templates(request: Request):
    return request.app.state.templates


def register(app: FastAPI) -> None:
    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return JSONResponse({"status": "ok"})

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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        repo = _repo(request)
        try:
            pubs = repo.list_publications()
            watched = [p for p in pubs if p.watched]
            all_issues = repo.list_issues()
            done_issues = [i for i in all_issues if i.status == IssueStatus.DONE]
            recent_issues = sorted(
                done_issues,
                key=lambda i: i.downloaded_at or datetime.min,
                reverse=True,
            )[:10]
            recent_jobs = repo.list_jobs(limit=10)
            stats = {
                "total_pubs": len(pubs),
                "watched_pubs": len(watched),
                "total_issues": len(all_issues),
                "downloaded_issues": len(done_issues),
            }
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
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            DownloadRepository(session).set_watched(code, True)
        return await _publication_row(request, code)

    @app.post("/publications/{code}/unwatch", response_class=HTMLResponse)
    async def unwatch_publication(request: Request, code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            DownloadRepository(session).set_watched(code, False)
        return await _publication_row(request, code)

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

    async def _issue_row(request: Request, code: str, issue_code: str) -> HTMLResponse:
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return HTMLResponse("Publication not found", status_code=404)
            issue = repo.get_issue_by_code(issue_code, pub.id)
            if issue is None:
                return HTMLResponse("Issue not found", status_code=404)
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

    @app.post("/publications/{code}/poll", response_class=HTMLResponse)
    async def poll_single(request: Request, code: str):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        token = load_token()
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

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_list(request: Request):
        repo = _repo(request)
        try:
            jobs = repo.list_jobs(limit=100)
            return _templates(request).TemplateResponse(
                request, "jobs.html", {"jobs": jobs}
            )
        finally:
            repo.session.close()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        repo = _repo(request)
        try:
            settings = {
                "poll_interval": repo.get_setting("poll_interval", "360"),
                "workers": repo.get_setting("workers", "4"),
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
    ):
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_setting("poll_interval", str(poll_interval))
            repo.set_setting("workers", str(workers))

        csrf = generate_csrf_token(request)
        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            {
                "settings": {
                    "poll_interval": str(poll_interval),
                    "workers": str(workers),
                },
                "csrf_token": csrf,
                "saved": True,
            },
        )
