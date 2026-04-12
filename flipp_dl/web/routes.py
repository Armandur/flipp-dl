"""FastAPI route definitions for flipp-dl web UI."""

from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..api import FlippClient
from ..config import load_token
from ..db.models import IssueStatus
from ..db.repository import DownloadRepository
from ..db.session import get_session
from ..scheduler import poll_publications


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
        finally:
            repo.session.close()

        return _templates(request).TemplateResponse(
            request,
            "dashboard.html",
            {
                "stats": stats,
                "recent_issues": recent_issues,
                "recent_jobs": recent_jobs,
            },
        )

    # ------------------------------------------------------------------
    # Publications
    # ------------------------------------------------------------------

    @app.get("/publications", response_class=HTMLResponse)
    async def publications_list(request: Request):
        repo = _repo(request)
        try:
            pubs = repo.list_publications()
            pubs.sort(key=lambda p: p.name)
        finally:
            repo.session.close()

        return _templates(request).TemplateResponse(
            request, "publications.html", {"publications": pubs}
        )

    @app.post("/publications/{code}/watch", response_class=HTMLResponse)
    async def watch_publication(request: Request, code: str):
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_watched(code, True)
        return await _publication_row(request, code)

    @app.post("/publications/{code}/unwatch", response_class=HTMLResponse)
    async def unwatch_publication(request: Request, code: str):
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_watched(code, False)
        return await _publication_row(request, code)

    async def _publication_row(request: Request, code: str) -> HTMLResponse:
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
        finally:
            repo.session.close()
        return _templates(request).TemplateResponse(
            request, "publication_row.html", {"publication": pub}
        )

    @app.post("/publications/{code}/poll", response_class=HTMLResponse)
    async def poll_single(request: Request, code: str):
        """Immediately trigger a poll for new issues on one publication."""
        token = load_token()
        if not token:
            return JSONResponse({"error": "No token configured"}, status_code=400)

        client = FlippClient(token)
        workers = int(os.environ.get("FLIPP_WORKERS", "4"))
        output = request.app.state.output_root
        factory = request.app.state.session_factory

        # Run synchronously in request handler (acceptable for one-off trigger)
        poll_publications(client, factory, output, workers)

        return HTMLResponse("<span>Polled ✓</span>")

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_list(request: Request):
        repo = _repo(request)
        try:
            jobs = repo.list_jobs(limit=100)
        finally:
            repo.session.close()

        return _templates(request).TemplateResponse(
            request, "jobs.html", {"jobs": jobs}
        )

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
        finally:
            repo.session.close()

        return _templates(request).TemplateResponse(
            request, "settings.html", {"settings": settings}
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(
        request: Request,
        poll_interval: int = Form(360),
        workers: int = Form(4),
    ):
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            repo.set_setting("poll_interval", str(poll_interval))
            repo.set_setting("workers", str(workers))

        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            {
                "settings": {
                    "poll_interval": str(poll_interval),
                    "workers": str(workers),
                },
                "saved": True,
            },
        )
