"""HTTP client for the Flipp API."""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .models import Publication

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) "
    "Gecko/20100101 Firefox/123.0"
)
DEFAULT_TIMEOUT = 30


class FlippError(Exception):
    """Raised when the Flipp API returns an unexpected response."""


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
        session: Optional[requests.Session] = None,
    ) -> None:
        self.token = token
        self.user_uuid = user_uuid
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

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
        publications = [Publication.from_api(p) for p in data["publications"]]
        logger.info("Fetched %d publications", len(publications))
        return publications

    def fetch_issue_pdf_urls(
        self, publication_code: str, issue_code: str
    ) -> list[str]:
        """Return the ordered list of PDF URLs for a single issue."""
        params = {"pubid": publication_code, "eid": issue_code}
        response = self.session.get(
            self.READER_URL, params=params, timeout=self.timeout
        )
        response.raise_for_status()
        data = response.json()
        if "pageGroups" not in data:
            raise FlippError(
                f"Unexpected response for publication={publication_code} "
                f"issue={issue_code}: missing 'pageGroups'"
            )
        return [
            page["pdf"]
            for group in data["pageGroups"]
            for page in group["pages"]
        ]

    def download_pdf(self, url: str) -> bytes:
        """Download a single PDF and return its raw bytes."""
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.content

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
        response = self.session.post(
            self.API_URL, json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        return response.json()
