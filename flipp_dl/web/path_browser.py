"""Secure directory browsing within the application's managed roots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath

from fastapi import FastAPI
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class BrowseRoot:
    id: str
    path: Path


def browse_roots(output_root: Path, db_path: Path) -> list[BrowseRoot]:
    """Return the unique roots exposed by the directory browser."""
    candidates = (("output", output_root), ("data", db_path.parent))
    roots: list[BrowseRoot] = []
    seen: set[Path] = set()
    for root_id, path in candidates:
        resolved = Path(path).resolve()
        if resolved not in seen:
            roots.append(BrowseRoot(root_id, resolved))
            seen.add(resolved)
    return roots


def _root(roots: list[BrowseRoot], root_id: str) -> BrowseRoot | None:
    return next((root for root in roots if root.id == root_id), None)


def resolve_relative_directory(
    roots: list[BrowseRoot], root_id: str, relative: str
) -> Path | None:
    """Resolve an existing directory without allowing root escapes."""
    root = _root(roots, root_id)
    pure = PurePath(relative)
    if root is None or pure.is_absolute() or ".." in pure.parts:
        return None
    try:
        target = (root.path / relative).resolve()
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(root.path) or not target.is_dir():
        return None
    return target


def directory_payload(
    roots: list[BrowseRoot], root_id: str, relative: str
) -> dict | None:
    """Build one safe directory listing for the JSON endpoint."""
    root = _root(roots, root_id)
    current = resolve_relative_directory(roots, root_id, relative)
    if root is None or current is None:
        return None

    directories = []
    try:
        entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        entries = []
    for entry in entries:
        try:
            resolved = entry.resolve()
            if not entry.is_dir() or not resolved.is_relative_to(root.path):
                continue
        except (OSError, RuntimeError):
            continue
        directories.append(
            {
                "name": entry.name,
                "path": str(resolved.relative_to(root.path)),
                "writable": os.access(resolved, os.W_OK),
            }
        )

    relative_path = current.relative_to(root.path)
    return {
        "root": root.id,
        "root_path": str(root.path),
        "path": "" if str(relative_path) == "." else str(relative_path),
        "absolute_path": str(current),
        "parent": None if current == root.path else str(relative_path.parent),
        "writable": os.access(current, os.W_OK),
        "directories": directories,
    }


def register(app: FastAPI) -> None:
    """Register the directory-listing route."""

    @app.get("/settings/directories")
    def list_directories(root: str, path: str = ""):
        roots = browse_roots(app.state.output_root, app.state.db_path)
        # Keep an explicit route-boundary check in addition to the module's
        # check inside directory_payload. A hand-written URL never reaches
        # directory iteration unless it has passed containment here first.
        if resolve_relative_directory(roots, root, path) is None:
            return JSONResponse({}, status_code=403)
        payload = directory_payload(roots, root, path)
        if payload is None:
            return JSONResponse({}, status_code=403)
        return JSONResponse(payload)
