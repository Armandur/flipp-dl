"""Domain models for flipp-dl."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Candidate keys where Egmont's API *might* expose the short publication
# code (the 2–5 letter acronym like "HBR" / "UVH" that the CDN uses for
# cover art URLs). We try these in order, then fall back to a heuristic.
_SHORT_CODE_KEYS = (
    "publicationCode",
    "shortCode",
    "shortcode",
    "code",
    "abbreviation",
    "acronym",
    "pubCode",
)
_SHORT_CODE_RE = re.compile(r"^[A-Z]{2,6}$")


def _extract_short_code(data: dict[str, Any]) -> str | None:
    """Best-effort lookup of a 2–6 letter uppercase publication code.

    Tries known key names first, then falls back to scanning top-level
    string values for something that looks like an acronym.
    """
    for key in _SHORT_CODE_KEYS:
        val = data.get(key)
        if isinstance(val, str) and _SHORT_CODE_RE.match(val):
            return val
    # Heuristic fallback: any top-level string value that matches the pattern.
    for val in data.values():
        if isinstance(val, str) and _SHORT_CODE_RE.match(val):
            return val
    return None


@dataclass(frozen=True)
class Category:
    id: int
    name: str


@dataclass(frozen=True)
class Issue:
    custom_code: str
    issue_name: str
    issue_date: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Issue:
        return cls(
            custom_code=data["customIssueCode"],
            issue_name=data.get("issueName", ""),
            issue_date=data.get("issueDate", ""),
        )


@dataclass
class Publication:
    custom_code: str
    name: str
    short_code: str | None = None
    categories: list[Category] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def num_issues(self) -> int:
        return len(self.issues)

    def has_category(self, category_id: int) -> bool:
        return any(c.id == category_id for c in self.categories)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Publication:
        return cls(
            custom_code=data["customPublicationCode"],
            name=data["name"],
            short_code=_extract_short_code(data),
            categories=[
                Category(id=c["id"], name=c["name"]) for c in data.get("categories", [])
            ],
            issues=[Issue.from_api(i) for i in data.get("issues", [])],
        )
