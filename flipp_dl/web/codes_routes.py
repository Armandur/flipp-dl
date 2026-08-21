"""Catalogue and code-backup routes for the settings page.

The web counterpart to the CLI's ``--import-catalog``, ``--export-codes``
and ``--import-backup``. Both sides share :mod:`flipp_dl.codes`, so a file
means the same thing whichever way it is fed in.

JSON goes both ways: the export is a download (GET with a
``Content-Disposition`` attachment), the two imports are file uploads
(multipart POST). The CSRF token rides along as a hidden form field, since
``check_csrf_form`` reads it out of the same parsed form as the file.

Kept out of ``routes.py`` (already 1400+ lines) per TASK-1292.
"""

from __future__ import annotations

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
from ..db.repository import DownloadRepository
from ..db.session import get_session
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
}


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
