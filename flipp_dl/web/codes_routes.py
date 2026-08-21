"""Catalogue, code-backup and edition routes for the settings page.

The web counterpart to the CLI's ``--import-catalog``, ``--export-codes``,
``--import-backup``, ``--discover-editions`` and ``--import-editions``. Both sides share :mod:`flipp_dl.codes`, so a file
means the same thing whichever way it is fed in.

JSON goes both ways: the export is a download (GET with a
``Content-Disposition`` attachment), the two imports are file uploads
(multipart POST). The CSRF token rides along as a hidden form field, since
``check_csrf_form`` reads it out of the same parsed form as the file.

The two edition runs make one HTTP call per publication (or per edition),
far too slow for a request, so they are queued as jobs and drained by
:func:`flipp_dl.editions.run_editions_queue` in the scheduler. The settings
page polls a status partial while one is running.

Kept out of ``routes.py`` (already 1400+ lines) per TASK-1292.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from ..codes import (
    CodeFileError,
    build_backup_payload,
    dump_backup_payload,
    import_catalog,
    parse_backup,
    parse_catalog,
    restore_backup,
)
from ..db.models import JobStatus
from ..db.repository import DownloadRepository
from ..db.session import get_session
from ..editions import JOB_DISCOVER, JOB_IMPORT, JOB_TYPES, parse_editions
from . import i18n
from .auth import check_csrf_form
from .routes import _mark_for_translation as _

logger = logging.getLogger(__name__)

# Biggest catalogue we have is well under a megabyte; the cap keeps a stray
# upload from being read into memory in full.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

_FILE_ERROR_MESSAGES = {
    "unreadable": _("The file could not be read - it is not valid JSON."),
    "not_a_list": _(
        "This does not look like a catalogue file (expected a list of publications)."
    ),
    "not_an_object": _(
        "This does not look like a backup file (expected an object with "
        "publications and issues)."
    ),
    "empty": _("No file was selected."),
    "too_large": _("The file is too large."),
    "no_editions": _("The file contains no editions."),
}

# A job in one of these states is still going to change, so the status
# partial keeps polling; anything else is its final word.
_ACTIVE_JOB_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


def _error(request: Request, reason: str) -> HTMLResponse:
    """Render the error partial with a translated, non-technical message."""
    lang = i18n.get_language(request)
    message = i18n.translate(
        lang, _FILE_ERROR_MESSAGES.get(reason, _FILE_ERROR_MESSAGES["unreadable"])
    )
    return request.app.state.templates.TemplateResponse(
        request, "codes_result.html", {"error": message, "result": None, "kind": ""}
    )


async def _read_upload(upload: UploadFile | None) -> bytes | str:
    """Return the uploaded bytes, or a reason string when unusable."""
    if upload is None or not upload.filename:
        return "empty"
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        return "empty"
    if len(data) > MAX_UPLOAD_BYTES:
        return "too_large"
    return data


def register(app: FastAPI) -> None:
    """Attach the catalogue/backup routes to *app*."""

    def _templates(request: Request):
        return request.app.state.templates

    @app.get("/settings/export-codes")
    async def export_codes(request: Request):
        """Download every pubid and eid as a JSON backup.

        A plain download, not an HTMX swap - the browser saves the file.
        """
        with get_session(request.app.state.session_factory) as session:
            payload = build_backup_payload(session)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        filename = f"flipp-koder-{stamp}.json"
        return Response(
            content=dump_backup_payload(payload),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/settings/import-catalog", response_class=HTMLResponse)
    async def import_catalog_route(
        request: Request,
        catalog: Annotated[UploadFile | None, File()] = None,
        unlisted: Annotated[UploadFile | None, File()] = None,
    ):
        """Add missing publications from an uploaded catalogue file.

        The optional second file is the unlisted companion the CLI picks up
        from disk next to the catalogue; an upload has no folder to look in,
        so it gets its own input.
        """
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        data = await _read_upload(catalog)
        if isinstance(data, str):
            return _error(request, data)
        # The unlisted companion is optional: no file at all is fine, but a
        # file we could not use is an error rather than a silent skip.
        unlisted_data = await _read_upload(unlisted)
        if unlisted_data == "too_large":
            return _error(request, "too_large")
        try:
            entries = parse_catalog(data)
            unlisted_entries = (
                [] if isinstance(unlisted_data, str) else parse_catalog(unlisted_data)
            )
        except CodeFileError as exc:
            return _error(request, exc.reason)

        with get_session(request.app.state.session_factory) as session:
            result = import_catalog(
                DownloadRepository(session), entries, unlisted_entries
            )
        return _templates(request).TemplateResponse(
            request,
            "codes_result.html",
            {"error": None, "result": result, "kind": "catalog"},
        )

    @app.post("/settings/import-backup", response_class=HTMLResponse)
    async def import_backup_route(
        request: Request,
        backup: Annotated[UploadFile | None, File()] = None,
    ):
        """Restore publications and issue codes from an uploaded backup."""
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        data = await _read_upload(backup)
        if isinstance(data, str):
            return _error(request, data)
        try:
            payload = parse_backup(data)
        except CodeFileError as exc:
            return _error(request, exc.reason)

        with get_session(request.app.state.session_factory) as session:
            result = restore_backup(DownloadRepository(session), payload)
        return _templates(request).TemplateResponse(
            request,
            "codes_result.html",
            {"error": None, "result": result, "kind": "backup"},
        )

    # ------------------------------------------------------------------
    # Editions - queued as jobs, drained by the scheduler
    # ------------------------------------------------------------------

    def _editions_status(request: Request, error: str | None = None) -> HTMLResponse:
        """Render the latest editions job, with its progress or result.

        An *error* is rendered alongside the job rather than in place of it:
        a bad upload must not silently stop the poll of a run already in
        flight.
        """
        with get_session(request.app.state.session_factory) as session:
            job = DownloadRepository(session).latest_job_of_types(JOB_TYPES)
            view = None
            if job is not None:
                try:
                    payload = json.loads(job.payload)
                except (TypeError, ValueError):
                    payload = {}
                view = {
                    "id": job.id,
                    "type": job.job_type,
                    "status": job.status,
                    "error": job.error_message,
                    "progress": payload.get("progress") or {},
                    "result": payload.get("result") or {},
                    "active": job.status in _ACTIVE_JOB_STATUSES,
                }
        return _templates(request).TemplateResponse(
            request, "editions_status.html", {"job": view, "error": error}
        )

    def _editions_error(request: Request, reason: str) -> HTMLResponse:
        """Render a file error into the status box without stopping the poll."""
        message = i18n.translate(
            i18n.get_language(request),
            _FILE_ERROR_MESSAGES.get(reason, _FILE_ERROR_MESSAGES["unreadable"]),
        )
        return _editions_status(request, message)

    def _queue_editions_job(request: Request, job_type: str, payload: dict):
        """Queue *job_type* unless a run is already waiting or working.

        Returns ``None`` when a job was queued, or the busy status partial
        when one is already in flight - two concurrent discovery runs would
        just fetch the same editions twice.
        """
        with get_session(request.app.state.session_factory) as session:
            repo = DownloadRepository(session)
            latest = repo.latest_job_of_types(JOB_TYPES)
            if latest is not None and latest.status in _ACTIVE_JOB_STATUSES:
                return _editions_status(request)
            repo.create_job(job_type, payload)
        return None

    @app.get("/settings/editions-status", response_class=HTMLResponse)
    async def editions_status(request: Request):
        """Poll target for the running editions job (HTMX)."""
        return _editions_status(request)

    @app.post("/settings/discover-editions", response_class=HTMLResponse)
    async def discover_editions_route(request: Request):
        """Queue a full edition discovery run across every publication."""
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)
        busy = _queue_editions_job(request, JOB_DISCOVER, {})
        return busy or _editions_status(request)

    @app.post("/settings/import-editions", response_class=HTMLResponse)
    async def import_editions_route(
        request: Request,
        editions: Annotated[UploadFile | None, File()] = None,
    ):
        """Queue an import of unlisted editions from an uploaded file."""
        if not await check_csrf_form(request):
            return HTMLResponse("CSRF validation failed", status_code=400)

        data = await _read_upload(editions)
        if isinstance(data, str):
            return _editions_error(request, data)
        try:
            entries = parse_editions(data)
        except CodeFileError as exc:
            return _editions_error(request, exc.reason)
        if not entries:
            return _editions_error(request, "no_editions")

        busy = _queue_editions_job(request, JOB_IMPORT, {"input": {"entries": entries}})
        return busy or _editions_status(request)
