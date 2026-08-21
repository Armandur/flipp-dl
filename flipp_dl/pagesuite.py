"""HTTP client for PageSuite's open edition-list endpoint (TASK-1439).

``editionshtml5_json.aspx`` lists every edition of a publication - including
the ones the Flipp app hides - with no authentication. It takes the PageSuite
``publicationguid``, which is the same value flipp-dl already stores as
:attr:`Publication.custom_code` (``== customPublicationCode``). The returned
``@editionguid`` is the same ``eid`` the reader API uses, so an edition
discovered here maps straight onto an :class:`Issue.custom_code` without any
translation - see ``docs`` / backlog doc 01M0GM0G for the verification.
"""

from __future__ import annotations

import logging

import requests

from .api import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, build_session
from .models import Issue

logger = logging.getLogger(__name__)


class PageSuiteError(Exception):
    """Raised when the PageSuite edition list is missing or malformed."""


def _normalise_date(raw: str) -> str:
    """PageSuite serves dates as ``M/D/YYYY``; store them as ISO ``YYYY-MM-DD``.

    Keeps the raw string on anything that does not parse, so a format
    change downgrades to "shown verbatim" rather than dropping the date.
    """
    parts = raw.strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        month, day, year = parts
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return raw.strip()


def _editions_from_payload(data: dict) -> list[Issue]:
    """Turn the ``{"editions": {"edition": [...]}}`` payload into issues."""
    editions = data.get("editions")
    if not isinstance(editions, dict):
        return []
    raw = editions.get("edition", [])
    if isinstance(raw, dict):
        # A publication with a single edition is not wrapped in a list.
        raw = [raw]
    if not isinstance(raw, list):
        return []
    issues: list[Issue] = []
    for edition in raw:
        if not isinstance(edition, dict):
            continue
        guid = edition.get("@editionguid")
        if not guid:
            continue
        issues.append(
            Issue(
                custom_code=guid,
                issue_name=edition.get("@name", ""),
                issue_date=_normalise_date(edition.get("@date", "")),
            )
        )
    return issues


def _iso_from_ddmmyyyy(raw: str) -> str:
    """replica serves editionName as ``D/M/YYYY``; store ISO ``YYYY-MM-DD``."""
    parts = raw.strip().split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        day, month, year = parts
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return raw.strip()


class EditionMetadata:
    """The bits of an edition the replica API exposes for a known eid."""

    def __init__(self, publication_guid: str, edition_name: str, iso_date: str):
        self.publication_guid = publication_guid
        self.edition_name = edition_name
        self.iso_date = iso_date


class PageSuiteClient:
    """Reads the open PageSuite edition list. No authentication needed."""

    EDITIONS_URL = "https://reader.flipp.se/html5/editionshtml5_json.aspx"
    REPLICA_URL = "https://api.replica.pagesuite.com/edition/{eid}/pages"

    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self.session = session or build_session()

    def fetch_editions(
        self, publication_guid: str, *, maxnumber: int = 5000
    ) -> list[Issue]:
        """Return every edition PageSuite lists for *publication_guid*.

        *publication_guid* is the PageSuite pubid, stored by flipp-dl as
        ``Publication.custom_code``. ``maxnumber`` bounds the response;
        5000 has always returned the full back catalogue.
        """
        params = {"publicationguid": publication_guid, "maxnumber": maxnumber}
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        response = self.session.get(
            self.EDITIONS_URL, params=params, headers=headers, timeout=self.timeout
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise PageSuiteError(
                f"Failed to fetch editions for {publication_guid}: {exc}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise PageSuiteError(
                f"PageSuite returned non-JSON for {publication_guid}"
            ) from exc
        issues = _editions_from_payload(data)
        logger.info(
            "PageSuite listed %d editions for %s", len(issues), publication_guid
        )
        return issues

    def fetch_edition_metadata(self, eid: str) -> EditionMetadata | None:
        """Resolve a single eid via the replica API (no authentication).

        Returns the edition's real ``publicationGuid`` (useful when an eid
        was found without knowing its publication) plus its name/date, or
        ``None`` if the eid is dead or the response is malformed. Used by the
        edition importer to attach shadow eids - the ones editionshtml5_json
        never lists - to the right publication (TASK-1437).
        """
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        try:
            response = self.session.get(
                self.REPLICA_URL.format(eid=eid), headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return None
        pub = data.get("publicationGuid")
        if not pub:
            return None
        name = data.get("editionName", "") or ""
        return EditionMetadata(pub, name, _iso_from_ddmmyyyy(name))
