"""HTTP client for the Komga API (TASK-1326, Komga-integration nivå 1).

Only the two calls level 1 needs: listing libraries (to populate the
settings dropdown) and triggering a library scan (to make Komga notice a
fresh download without waiting for its own scheduled scan). Follows the
same session/retry style as :mod:`flipp_dl.api`.
"""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class KomgaError(Exception):
    """Raised when the Komga API returns an unexpected response."""


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
        """Return the raw library list from ``GET /api/v1/libraries``."""
        response = self._request("get", "/api/v1/libraries")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KomgaError(f"Failed to list Komga libraries: {exc}") from exc
        return response.json()

    def scan_library(self, library_id: str) -> None:
        """Trigger a scan of *library_id* via ``POST .../scan``.

        Komga accepts this request and returns immediately (the scan runs
        asynchronously on its side) - there is nothing to wait for here.
        """
        response = self._request("post", f"/api/v1/libraries/{library_id}/scan")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KomgaError(
                f"Failed to trigger scan for library {library_id}: {exc}"
            ) from exc

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

    def _request(self, method: str, path: str) -> requests.Response:
        url = f"{self.url}{path}"
        try:
            return self.session.request(
                method,
                url,
                headers=self._headers(),
                auth=self._auth(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise KomgaError(f"Komga request to {url} failed: {exc}") from exc
