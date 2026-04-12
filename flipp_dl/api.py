"""HTTP client for the Flipp API."""

from __future__ import annotations

import io
import logging
from typing import BinaryIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Publication

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) "
    "Gecko/20100101 Firefox/123.0"
)
DEFAULT_TIMEOUT = 30
DEFAULT_CHUNK_SIZE = 64 * 1024


class FlippError(Exception):
    """Raised when the Flipp API returns an unexpected response."""


def build_session() -> requests.Session:
    """Return a :class:`requests.Session` with sensible retry defaults."""
    session = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # NOTE: requests.Session() pre-populates headers with its own default
    # User-Agent ("python-requests/X.Y.Z"), which Flipp's API rejects with
    # 403 Forbidden. Assign (not setdefault) so our browser UA wins.
    session.headers["User-Agent"] = DEFAULT_USER_AGENT
    return session


class FlippClient:
    """Thin wrapper around the Flipp endpoints used by flipp-dl."""

    API_URL = "https://flippapi.egmontservice.com/api/refreshsignintoken"
    READER_URL = "https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx"

    def __init__(
        self,
        token: str,
        *,
        user_uuid: str = "dummy",
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token
        self.user_uuid = user_uuid
        self.timeout = timeout
        self.session = session or build_session()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_publications(self) -> list[Publication]:
        """Return the publications the current token has access to."""
        data = self._refresh_sign_in_token()
        if "publications" not in data:
            raise FlippError(
                "Flipp API response missing 'publications'. Is the token "
                "valid? Got keys: " + ", ".join(data.keys())
            )
        raw = data["publications"]
        # Log the shape of the first publication once per process so we
        # can spot new/renamed fields (e.g. the short publication code
        # used by the CDN for cover art).
        if raw and not getattr(self, "_logged_schema", False):
            sample = raw[0]
            logger.info(
                "Flipp publication schema (first row) keys: %s",
                sorted(sample.keys()) if isinstance(sample, dict) else type(sample),
            )
            self._logged_schema = True
        publications = [Publication.from_api(p) for p in raw]
        logger.info("Fetched %d publications", len(publications))
        return publications

    def fetch_issue_pdf_urls(self, publication_code: str, issue_code: str) -> list[str]:
        """Return the ordered list of PDF URLs for a single issue."""
        params = {"pubid": publication_code, "eid": issue_code}
        response = self.session.get(
            self.READER_URL, params=params, timeout=self.timeout
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise FlippError(
                f"Failed to fetch PDF URLs for {publication_code}/{issue_code}: {exc}"
            ) from exc
        data = response.json()
        if "pageGroups" not in data:
            raise FlippError(
                f"Unexpected response for publication={publication_code} "
                f"issue={issue_code}: missing 'pageGroups'"
            )
        return [page["pdf"] for group in data["pageGroups"] for page in group["pages"]]

    def download_pdf_to(self, url: str, destination: BinaryIO) -> int:
        """Stream a PDF into *destination* and return the number of bytes."""
        total = 0
        with self.session.get(url, timeout=self.timeout, stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=DEFAULT_CHUNK_SIZE):
                if chunk:
                    destination.write(chunk)
                    total += len(chunk)
        return total

    def download_pdf(self, url: str) -> bytes:
        """Download a PDF and return its raw bytes (streamed under the hood)."""
        buf = io.BytesIO()
        self.download_pdf_to(url, buf)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_sign_in_token(self) -> dict:
        payload = {
            "email": "",
            "password": "",
            "token": self.token,
            "languageCulture": "sv-SE",
            "appId": "se.egmontmagasiner.flipp",
            "appVersion": "Landing Page",
            "uuid": self.user_uuid,
            "os": "Firefox / Windows",
        }
        # Pass headers per-request so we override any session defaults
        # (requests.Session pre-populates User-Agent, Accept, etc.).
        # Egmont appears to require a browser-like request signature.
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://tidningar.flipp.se",
            "Referer": "https://tidningar.flipp.se/",
        }
        logger.debug(
            "POST %s (token len=%d, uuid=%s)",
            self.API_URL,
            len(self.token) if self.token else 0,
            self.user_uuid,
        )
        response = self.session.post(
            self.API_URL, json=payload, headers=headers, timeout=self.timeout
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = response.status_code
            # Surface a short body snippet to help diagnose why Flipp is unhappy.
            body_snippet = (response.text or "")[:300].replace("\n", " ")
            logger.error("Flipp API %s response: %s", status, body_snippet or "<empty>")
            if status == 403:
                raise FlippError(
                    "Flipp API returned 403 Forbidden. This usually means the "
                    "token is invalid or expired – re-fetch it from the browser "
                    "(Network tab → refreshsignintoken) and update FLIPP_TOKEN. "
                    f"Response body: {body_snippet or '<empty>'}"
                ) from exc
            raise FlippError(f"Flipp API HTTP error {status}: {exc}") from exc
        return response.json()
