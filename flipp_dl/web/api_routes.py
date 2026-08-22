"""JSON API endpoints for flipp-dl.

Read-only REST/JSON views over the same data the HTML pages render, for
programmatic access (scripts, dashboards, monitoring). These endpoints go
through the same :class:`~flipp_dl.web.auth.AuthMiddleware` as the HTML
routes, but an unauthenticated request under ``/api/`` gets a JSON 401
rather than a redirect to the login form - a script cannot fill in a
form.

Kept out of ``routes.py`` (already 700+ lines) per TASK-1292.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..db.models import JobStatus
from ..db.repository import DownloadRepository

_JOB_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.DONE,
    JobStatus.ERROR,
    JobStatus.RETRY_PENDING,
)


def _repo(request: Request) -> DownloadRepository:
    session = request.app.state.session_factory()
    return DownloadRepository(session)


def _publication_json(pub) -> dict:
    """Serialize a publication row using the pre-aggregated counts.

    Mirrors ``list_publications()``'s ``num_issues``/``num_downloaded``
    setters - never touches ``pub.issues`` (TASK-1338 removed that
    eager-load for the list view).
    """
    return {
        "custom_code": pub.custom_code,
        "name": pub.name,
        "watched": pub.watched,
        "num_issues": pub.num_issues,
        "num_downloaded": pub.num_downloaded,
        "next_issue_date": pub.next_issue_date,
    }


def _issue_json(issue) -> dict:
    return {
        "custom_code": issue.custom_code,
        "issue_name": issue.issue_name,
        "issue_date": issue.issue_date,
        "status": issue.status,
        "downloaded_at": (
            issue.downloaded_at.isoformat() if issue.downloaded_at else None
        ),
    }


def _job_json(job) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_message": job.error_message,
    }


def register(app: FastAPI) -> None:
    @app.get("/api/publications")
    async def api_publications_list(request: Request):
        repo = _repo(request)
        try:
            pubs = repo.list_publications()
            pubs.sort(key=lambda p: p.name.lower())
            return JSONResponse([_publication_json(p) for p in pubs])
        finally:
            repo.session.close()

    @app.get("/api/publications/{code}")
    async def api_publication_detail(request: Request, code: str):
        repo = _repo(request)
        try:
            pub = repo.get_publication(code)
            if pub is None:
                return JSONResponse(
                    {"error": "publication not found", "custom_code": code},
                    status_code=404,
                )
            issues = sorted(pub.issues, key=lambda i: i.issue_date or "", reverse=True)
            body = {
                "custom_code": pub.custom_code,
                "name": pub.name,
                "watched": pub.watched,
                "next_issue_date": pub.next_issue_date,
                "issues": [_issue_json(i) for i in issues],
            }
            return JSONResponse(body)
        finally:
            repo.session.close()

    @app.get("/api/jobs")
    async def api_jobs_list(request: Request, status: str = ""):
        selected = status if status in _JOB_STATUSES else ""
        repo = _repo(request)
        try:
            jobs = repo.list_jobs(limit=100, status=selected or None)
            return JSONResponse([_job_json(j) for j in jobs])
        finally:
            repo.session.close()
