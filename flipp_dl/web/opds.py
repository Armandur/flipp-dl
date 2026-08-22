"""OPDS catalog feed generation - Atom 1.2 and JSON 2.0 (TASK-1294).

An OPDS reader (e.g. on a tablet) uses this to discover and fetch newly
downloaded issues automatically instead of the owner manually copying
files. Both OPDS versions describe the *same* catalog - a navigation
feed of watched publications, and per-publication acquisition feeds
listing their downloaded (status DONE) issues - so this module builds
one format-agnostic :class:`Feed` and hands it to either
:func:`render_atom` or :func:`render_json`. The feed is only ever
assembled once per request; only the serialization differs by format.

Acquisition links point at the *existing* file/cover routes in
``routes.py`` (``/publications/{code}/issues/{issue_code}/file`` etc.)
rather than serving files here - those routes already resolve through
``storage.resolve_safe_path`` before touching disk, so this module never
needs its own traversal guard for the PDFs themselves. It still calls
that same guard once per issue below, to decide *whether* to advertise
an acquisition link at all: a DB row can say DONE with a stale or
missing ``file_path`` (file deleted from disk, moved, etc.), and a feed
should never point a client at a link that 404s.

Auth: these feeds are mounted under ``/api/opds*`` in ``routes.py`` so
they go through the *existing* ``AuthMiddleware`` exactly like the rest
of ``/api/`` - an unauthenticated request gets a JSON 401 instead of an
HTML redirect to ``/login`` that no OPDS client could follow. This
reuses the session-cookie mechanism the whole UI already uses; it does
*not* add HTTP Basic auth, so a reader app that only speaks Basic (many
do) still can't authenticate when ``FLIPP_PASSWORD`` is set - only a
client able to drive the cookie-based ``/login`` POST, or a deployment
with no password set at all, can consume a protected feed. Adding Basic
auth support would require changes to ``auth.py``, out of scope here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request

from .. import storage
from ..db.models import IssueStatus
from ..db.repository import DownloadRepository

ATOM_CONTENT_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ATOM_ACQUISITION_CONTENT_TYPE = (
    "application/atom+xml;profile=opds-catalog;kind=acquisition"
)
JSON_CONTENT_TYPE = "application/opds+json"

_ATOM_NS = "http://www.w3.org/2005/Atom"
_OPDS_NS = "http://opds-spec.org/2010/catalog"

_REL_ACQUISITION = "http://opds-spec.org/acquisition"
_REL_SUBSECTION = "subsection"
_REL_THUMBNAIL = "http://opds-spec.org/image/thumbnail"
_REL_SELF = "self"
_REL_START = "start"

_COVER_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# ---------------------------------------------------------------------------
# Format-agnostic catalog representation
# ---------------------------------------------------------------------------


@dataclass
class FeedLink:
    rel: str
    href: str
    type: str
    title: str | None = None


@dataclass
class FeedEntry:
    id: str
    title: str
    updated: datetime
    links: list[FeedLink] = field(default_factory=list)
    summary: str | None = None


@dataclass
class Feed:
    id: str
    title: str
    updated: datetime
    kind: str  # "navigation" | "acquisition"
    self_link: FeedLink
    start_link: FeedLink
    entries: list[FeedEntry] = field(default_factory=list)


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _now() -> datetime:
    return datetime.now(UTC)


def _cover_media_type(cache_path: str) -> str:
    return _COVER_MEDIA_TYPES.get(Path(cache_path).suffix.lower(), "image/jpeg")


# ---------------------------------------------------------------------------
# Builders - shared catalog logic, called once per request
# ---------------------------------------------------------------------------


def build_root_feed(
    repo: DownloadRepository, request: Request, *, json_format: bool
) -> Feed:
    """Navigation feed: one entry per watched publication.

    Each entry links (``rel=subsection``) to that publication's
    acquisition feed - in the *same* format as this root feed, so a
    client that fetched the Atom root is never handed a JSON child link.
    """
    base = _base_url(request)
    prefix = "/api/opds2" if json_format else "/api/opds"
    acquisition_type = (
        JSON_CONTENT_TYPE if json_format else ATOM_ACQUISITION_CONTENT_TYPE
    )

    pubs = repo.list_publications(watched_only=True)
    pubs.sort(key=lambda p: p.name.lower())

    entries = []
    for pub in pubs:
        links = [
            FeedLink(
                rel=_REL_SUBSECTION,
                href=f"{base}{prefix}/{pub.custom_code}",
                type=acquisition_type,
                title=pub.name,
            )
        ]
        if pub.cover_cache_path:
            links.append(
                FeedLink(
                    rel=_REL_THUMBNAIL,
                    href=f"{base}/publications/{pub.custom_code}/cover",
                    type=_cover_media_type(pub.cover_cache_path),
                )
            )
        entries.append(
            FeedEntry(
                id=f"urn:flipp-dl:publication:{pub.custom_code}",
                title=pub.name,
                updated=_now(),
                links=links,
                summary=f"{pub.num_downloaded} av {pub.num_issues} nedladdade",
            )
        )

    self_href = f"{base}{prefix}"
    nav_type = JSON_CONTENT_TYPE if json_format else ATOM_CONTENT_TYPE
    return Feed(
        id="urn:flipp-dl:root",
        title="flipp-dl - bevakade publikationer",
        updated=_now(),
        kind="navigation",
        self_link=FeedLink(rel=_REL_SELF, href=self_href, type=nav_type),
        start_link=FeedLink(rel=_REL_START, href=self_href, type=nav_type),
        entries=entries,
    )


def build_publication_feed(
    repo: DownloadRepository,
    request: Request,
    output_root: Path,
    code: str,
    *,
    json_format: bool,
) -> Feed | None:
    """Acquisition feed: *code*'s downloaded issues, newest first.

    Returns ``None`` if *code* doesn't name a known publication - the
    caller turns that into a 404.
    """
    pub = repo.get_publication(code)
    if pub is None:
        return None

    base = _base_url(request)
    issues = repo.list_issues(publication_id=pub.id, status=IssueStatus.DONE.value)
    issues.sort(key=lambda i: i.issue_date or "", reverse=True)

    entries = []
    for issue in issues:
        if storage.resolve_safe_path(output_root, issue.file_path) is None:
            # DB says DONE but the file is gone or outside output_root -
            # never advertise an acquisition link that would 404.
            continue
        links = [
            FeedLink(
                rel=_REL_ACQUISITION,
                href=f"{base}/publications/{pub.custom_code}/issues/{issue.custom_code}/file",
                type="application/pdf",
                title=issue.issue_name,
            )
        ]
        if issue.cover_cache_path:
            links.append(
                FeedLink(
                    rel=_REL_THUMBNAIL,
                    href=f"{base}/publications/{pub.custom_code}/issues/{issue.custom_code}/cover",
                    type=_cover_media_type(issue.cover_cache_path),
                )
            )
        entries.append(
            FeedEntry(
                id=f"urn:flipp-dl:issue:{pub.custom_code}:{issue.custom_code}",
                title=issue.issue_name,
                updated=issue.downloaded_at or _now(),
                links=links,
            )
        )

    prefix = "/api/opds2" if json_format else "/api/opds"
    self_href = f"{base}{prefix}/{pub.custom_code}"
    root_href = f"{base}{prefix}"
    acquisition_type = (
        JSON_CONTENT_TYPE if json_format else ATOM_ACQUISITION_CONTENT_TYPE
    )
    nav_type = JSON_CONTENT_TYPE if json_format else ATOM_CONTENT_TYPE
    return Feed(
        id=f"urn:flipp-dl:publication:{pub.custom_code}",
        title=pub.name,
        updated=_now(),
        kind="acquisition",
        self_link=FeedLink(rel=_REL_SELF, href=self_href, type=acquisition_type),
        start_link=FeedLink(rel=_REL_START, href=root_href, type=nav_type),
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def render_atom(feed: Feed) -> bytes:
    """Serialize *feed* as an OPDS 1.2 Atom document."""
    root = ET.Element("feed", {"xmlns": _ATOM_NS, "xmlns:opds": _OPDS_NS})
    ET.SubElement(root, "id").text = feed.id
    ET.SubElement(root, "title").text = feed.title
    ET.SubElement(root, "updated").text = _iso(feed.updated)
    for link in (feed.self_link, feed.start_link):
        ET.SubElement(root, "link", rel=link.rel, href=link.href, type=link.type)
    for entry in feed.entries:
        entry_el = ET.SubElement(root, "entry")
        ET.SubElement(entry_el, "id").text = entry.id
        ET.SubElement(entry_el, "title").text = entry.title
        ET.SubElement(entry_el, "updated").text = _iso(entry.updated)
        if entry.summary:
            ET.SubElement(entry_el, "content", type="text").text = entry.summary
        for link in entry.links:
            attrs = {"rel": link.rel, "href": link.href, "type": link.type}
            if link.title:
                attrs["title"] = link.title
            ET.SubElement(entry_el, "link", **attrs)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _json_link(link: FeedLink) -> dict:
    body: dict = {"href": link.href, "type": link.type}
    if link.rel:
        body["rel"] = link.rel
    if link.title:
        body["title"] = link.title
    return body


def render_json(feed: Feed) -> dict:
    """Serialize *feed* as an OPDS 2.0 JSON document."""
    body = {
        "metadata": {
            "title": feed.title,
            "id": feed.id,
            "modified": _iso(feed.updated),
        },
        "links": [_json_link(feed.self_link), _json_link(feed.start_link)],
    }
    if feed.kind == "navigation":
        body["navigation"] = [
            {**_json_link(link), "title": entry.title}
            for entry in feed.entries
            for link in entry.links
            if link.rel == _REL_SUBSECTION
        ]
    else:
        publications = []
        for entry in feed.entries:
            publications.append(
                {
                    "metadata": {
                        "title": entry.title,
                        "identifier": entry.id,
                        "modified": _iso(entry.updated),
                    },
                    "links": [
                        _json_link(link)
                        for link in entry.links
                        if link.rel == _REL_ACQUISITION
                    ],
                    "images": [
                        _json_link(link)
                        for link in entry.links
                        if link.rel == _REL_THUMBNAIL
                    ],
                }
            )
        body["publications"] = publications
    return body
