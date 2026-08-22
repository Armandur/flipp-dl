"""HTTP client for the Komga API.

Level 1 (TASK-1326) added the two calls needed just to make Komga notice a
fresh download: listing libraries (to populate the settings dropdown) and
triggering a library scan. Level 2 (TASK-1327) adds everything needed to
push the metadata Flipp already has - since Swedish comics don't exist in
Comicvine/GCD, no external Komga metadata provider can fill this in for
us - and Flipp's official cover, so Komga shows something better than a
guess at the first page. Follows the same session/retry style as
:mod:`flipp_dl.api`.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import os
import re
from collections.abc import Iterable
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .web.html_sanitize import sanitize_html

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# How many seconds komga_sync polls Komga's book list for the book that
# should have appeared after the library scan it just triggered - Komga's
# scan is asynchronous, so the book isn't necessarily there yet.
DEFAULT_WAIT_SECONDS = 10

# Per-field feature flags (TASK-1327 acceptance criteria): each Komga API
# field name maps to the env var that can turn its push off, e.g.
# ``KOMGA_PUSH_SUMMARY=false`` for someone who edits summaries by hand in
# Komga's UI. Kept as flat dicts (rather than scattered `if` checks) so a
# field push is a single lookup - see :func:`filter_pushed_fields`.
PUBLICATION_METADATA_FIELDS: dict[str, str] = {
    "title": "KOMGA_PUSH_TITLE",
    "titleSort": "KOMGA_PUSH_TITLE",
    "summary": "KOMGA_PUSH_SUMMARY",
    "publisher": "KOMGA_PUSH_PUBLISHER",
    "language": "KOMGA_PUSH_LANGUAGE",
    "genres": "KOMGA_PUSH_GENRES",
    "tags": "KOMGA_PUSH_GENRES",
}

ISSUE_METADATA_FIELDS: dict[str, str] = {
    "title": "KOMGA_PUSH_ISSUE_TITLE",
    "number": "KOMGA_PUSH_NUMBER",
    "numberSort": "KOMGA_PUSH_NUMBER",
    "releaseDate": "KOMGA_PUSH_RELEASE_DATE",
}

# Feature flag for the cover-thumbnail push, checked the same way as the
# per-field flags above but not tied to a metadata dict entry.
PUSH_COVER_FLAG = "KOMGA_PUSH_COVER"

# Komga runs on Spring Boot, whose multipart default caps an uploaded file
# at 1 MB - a larger cover comes back as 413 (measured in production
# 2026-08-22: one 1.29 MB cover failed 72 jobs). Checking the size here
# turns a guaranteed round trip into a skipped one. Raise it alongside
# Komga's own SPRING_SERVLET_MULTIPART_MAXFILESIZE if that is raised.
DEFAULT_MAX_COVER_BYTES = 1_000_000

_NUMBER_RE = re.compile(r"(?:nr\.?\s*)?(\d+)", re.IGNORECASE)


def _flag_enabled(env_key: str) -> bool:
    """True unless *env_key* is explicitly set to a falsy value.

    Every push flag defaults to on - a fresh deployment behaves exactly
    like the acceptance criteria describe (push everything we have)
    until someone opts a specific field out.
    """
    return os.environ.get(env_key, "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def filter_pushed_fields(fields: dict, field_map: dict[str, str]) -> dict:
    """Drop ``None`` values and any field disabled via its feature flag.

    Shared by the series and issue metadata pushes so "only set what we
    have" (acceptance criteria) and the per-field opt-out are each
    implemented exactly once.
    """
    result = {}
    for key, value in fields.items():
        if value is None:
            continue
        flag = field_map.get(key)
        if flag is not None and not _flag_enabled(flag):
            continue
        result[key] = value
    return result


def cover_push_enabled() -> bool:
    """Whether the series-thumbnail push is enabled (``KOMGA_PUSH_COVER``)."""
    return _flag_enabled(PUSH_COVER_FLAG)


def max_cover_bytes() -> int:
    """Largest cover we will try to upload (``KOMGA_MAX_COVER_BYTES``).

    Zero or a negative value disables the check, for a Komga configured
    with no practical limit.
    """
    raw = os.environ.get("KOMGA_MAX_COVER_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_COVER_BYTES
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_COVER_BYTES


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def html_to_plain_text(html_body: str | None) -> str:
    """Convert Flipp's HTML blurb to plain text for Komga's ``summary``.

    Komga's comic-book-reader clients render ``summary`` as plain text,
    not HTML - pushing the sanitised-but-still-tagged markup used on the
    detail page (:mod:`flipp_dl.web.html_sanitize`) would show literal
    ``<p>``/``<strong>`` tags to anyone reading it in Komga. This reuses
    that sanitiser first (defence in depth - stripped before the tags
    themselves are stripped) and then removes every remaining tag,
    turning ``<br>``/``</p>`` into line breaks and collapsing the result.
    """
    if not html_body:
        return ""
    safe = sanitize_html(html_body)
    text = re.sub(r"(?i)<br\s*/?>", "\n", safe)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.strip().splitlines()).strip()


def parse_issue_number(issue_name: str | None) -> tuple[str | None, float | None]:
    """Extract a book number from *issue_name*, e.g. "Nr 12" -> ("12", 12.0).

    Returns ``(None, None)`` when no digits are found - callers then omit
    ``number``/``numberSort`` entirely and let Komga fall back to its own
    lexicographic ordering, per the level-2 implementation plan.
    """
    if not issue_name:
        return None, None
    match = _NUMBER_RE.search(issue_name)
    if not match:
        return None, None
    digits = match.group(1)
    return digits, float(digits)


class KomgaError(Exception):
    """Raised when a Komga API call fails or returns something unexpected.

    ``reason`` is a stable category the web layer maps to a short,
    non-technical message for the user (see ``komga_test_connection`` in
    :mod:`flipp_dl.web.routes`):

    - ``"unreachable"`` - the address didn't respond at all (connection
      refused, DNS failure, timeout).
    - ``"auth"`` - the request reached a server but was rejected as
      unauthenticated/forbidden (401/403).
    - ``"bad_response"`` - a response came back but its content doesn't
      look like it came from Komga (not JSON, or not the shape expected).
    - ``"other"`` - anything else (other HTTP errors, unexpected
      exceptions).

    The exception's own message (``str(exc)``) keeps the technical detail
    - the ``requests`` exception text - so it stays available in the logs
    for troubleshooting even though the user never sees it directly.
    """

    def __init__(self, message: str, *, reason: str = "other") -> None:
        super().__init__(message)
        self.reason = reason


def build_session() -> requests.Session:
    """Return a :class:`requests.Session` with sensible retry defaults."""
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class KomgaClient:
    """Thin wrapper around the Komga endpoints used by flipp-dl.

    Supports both HTTP Basic auth (older Komga instances / a regular user
    account) and the ``X-API-Key`` header (Komga >= 1.8's API keys). When
    both are configured, the API key wins.
    """

    def __init__(
        self,
        url: str,
        *,
        username: str = "",
        password: str = "",
        api_key: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or build_session()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_libraries(self) -> list[dict]:
        """Return the raw library list from ``GET /api/v1/libraries``.

        Unlike the other calls, this one also checks that the response
        looks like it actually came from Komga - it's the call behind the
        "Test connection" button, which may be pointed at any URL a user
        typed in, including one that answers with an HTML error page or
        some unrelated JSON API.
        """
        response = self._request("get", "/api/v1/libraries")
        self._raise_for_status(response, "Failed to list Komga libraries")
        try:
            data = response.json()
        except ValueError as exc:
            raise KomgaError(
                f"Komga response from {self.url}/api/v1/libraries was not valid JSON: {exc}",
                reason="bad_response",
            ) from exc
        if not isinstance(data, list):
            raise KomgaError(
                "Komga response from "
                f"{self.url}/api/v1/libraries did not look like a library list: {data!r}",
                reason="bad_response",
            )
        return data

    def scan_library(self, library_id: str) -> None:
        """Trigger a scan of *library_id* via ``POST .../scan``.

        Komga accepts this request and returns immediately (the scan runs
        asynchronously on its side) - there is nothing to wait for here.
        """
        response = self._request("post", f"/api/v1/libraries/{library_id}/scan")
        self._raise_for_status(
            response, f"Failed to trigger scan for library {library_id}"
        )

    def find_series_by_name(self, library_id: str, name: str) -> dict | None:
        """Find the series in *library_id* whose name exactly matches *name*.

        Uses Komga's search (``GET /api/v1/series?search=...``) to narrow
        the candidates, then requires an exact match against either the
        series' own ``name`` or its ``metadata.title`` - a substring hit
        from the search alone is not good enough to auto-map a
        publication. Returns ``None`` when nothing matches; callers treat
        that as "not mapped yet, try again next sync".
        """
        response = self._request(
            "get",
            "/api/v1/series",
            params={"search": name, "library_id": library_id},
        )
        self._raise_for_status(response, f"Failed to search Komga series for {name!r}")
        data = response.json()
        content = data.get("content", data) if isinstance(data, dict) else data
        for series in content:
            metadata = series.get("metadata") or {}
            if series.get("name") == name or metadata.get("title") == name:
                return series
        return None

    def patch_series_metadata(self, series_id: int | str, **fields) -> None:
        """``PATCH /api/v1/series/{id}/metadata`` with only the given fields."""
        if not fields:
            return
        response = self._request(
            "patch", f"/api/v1/series/{series_id}/metadata", json_body=fields
        )
        self._raise_for_status(
            response, f"Failed to update metadata for series {series_id}"
        )

    def list_series_books(self, series_id: int | str) -> list[dict]:
        """Return every book Komga has indexed for *series_id*."""
        response = self._request(
            "get",
            f"/api/v1/series/{series_id}/books",
            params={"size": 5000, "unpaged": "true"},
        )
        self._raise_for_status(response, f"Failed to list books for series {series_id}")
        data = response.json()
        return data.get("content", data) if isinstance(data, dict) else data

    def find_book_by_stems(
        self, series_id: int | str, stems: Iterable[str]
    ) -> dict | None:
        """Find the book in *series_id* whose filename stem is in *stems*.

        Two stems are passed by the caller - the plain filename and the
        disambiguated ``(kortkod)`` form (TASK-1349) - so this matches
        whichever one the download actually produced.
        """
        stem_set = set(stems)
        for book in self.list_series_books(series_id):
            name = book.get("name") or Path(book.get("url", "")).stem
            if name in stem_set:
                return book
        return None

    def patch_book_metadata(self, book_id: int | str, **fields) -> None:
        """``PATCH /api/v1/books/{id}/metadata`` with only the given fields."""
        if not fields:
            return
        response = self._request(
            "patch", f"/api/v1/books/{book_id}/metadata", json_body=fields
        )
        self._raise_for_status(
            response, f"Failed to update metadata for book {book_id}"
        )

    def get_book_read_progress(self, book_id: int | str) -> dict:
        """Return read status for *book_id* via ``GET /api/v1/books/{id}``.

        Komga embeds the current user's progress in the book resource's
        ``readProgress`` field, which is ``null``/absent until the book
        has been opened at least once. Normalises that into
        ``{"read": bool, "page": int, "completed": bool}`` - "read" is an
        alias for "completed" so callers (the daily sync in
        :mod:`flipp_dl.scheduler`, TASK-1328) don't need to know Komga's
        exact field name. A book never opened returns
        ``{"read": False, "page": 0, "completed": False}``.
        """
        response = self._request("get", f"/api/v1/books/{book_id}")
        self._raise_for_status(
            response, f"Failed to fetch read progress for book {book_id}"
        )
        data = response.json()
        progress = data.get("readProgress") or {}
        completed = bool(progress.get("completed", False))
        page = int(progress.get("page") or 0)
        return {"read": completed, "page": page, "completed": completed}

    def upload_series_thumbnail(
        self, series_id: int | str, content: bytes, filename: str
    ) -> None:
        """``POST /api/v1/series/{id}/thumbnails`` with *content* as the file.

        ``selected=true`` makes it the series' active thumbnail so it
        replaces whatever Komga guessed (usually the PDF's first page)
        instead of just being added alongside it.
        """
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"file": (filename, content, content_type)}
        response = self._request(
            "post",
            f"/api/v1/series/{series_id}/thumbnails",
            params={"selected": "true"},
            files=files,
        )
        self._raise_for_status(
            response, f"Failed to upload thumbnail for series {series_id}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        if self.api_key:
            return {"X-API-Key": self.api_key}
        return {}

    def _auth(self) -> tuple[str, str] | None:
        if self.api_key:
            return None
        if self.username:
            return (self.username, self.password)
        return None

    def _raise_for_status(self, response: requests.Response, action: str) -> None:
        """Turn a non-2xx *response* into a categorised :class:`KomgaError`.

        401/403 gets ``reason="auth"`` (login rejected) - everything else
        (other HTTP errors) gets ``reason="other"``, since ``requests``
        doesn't give us anything more specific to go on there.
        """
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            reason = "auth" if response.status_code in (401, 403) else "other"
            raise KomgaError(f"{action}: {exc}", reason=reason) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
    ) -> requests.Response:
        url = f"{self.url}{path}"
        try:
            return self.session.request(
                method,
                url,
                headers=self._headers(),
                auth=self._auth(),
                timeout=self.timeout,
                params=params,
                json=json_body,
                files=files,
            )
        except requests.Timeout as exc:
            raise KomgaError(
                f"Komga request to {url} timed out: {exc}", reason="unreachable"
            ) from exc
        except requests.ConnectionError as exc:
            raise KomgaError(
                f"Komga request to {url} failed: {exc}", reason="unreachable"
            ) from exc
        except requests.RequestException as exc:
            raise KomgaError(f"Komga request to {url} failed: {exc}") from exc
