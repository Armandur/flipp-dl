"""Domain models for flipp-dl."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Swedish ("Nästa nummer kommer den 2026-04-16") and Norwegian
# ("Neste nummer kommer …") release-date strings embedded at the end
# of the Flipp `description` HTML. The optional "den" matches the
# Swedish-only flavour.
_NEXT_ISSUE_RE = re.compile(
    r"(?:Nästa|Neste)\s+nummer\s+kommer(?:\s+den)?\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _extract_next_issue_date(description: str | None) -> str | None:
    if not description:
        return None
    m = _NEXT_ISSUE_RE.search(description)
    return m.group(1) if m else None


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
    cover_url: str | None = None
    description: str | None = None
    next_issue_date: str | None = None
    categories: list[Category] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def num_issues(self) -> int:
        return len(self.issues)

    def has_category(self, category_id: int) -> bool:
        return any(c.id == category_id for c in self.categories)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Publication:
        cover_url = data.get("latestCoverImageUrl")
        if not isinstance(cover_url, str) or not cover_url.strip():
            cover_url = None
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            description = None
        return cls(
            custom_code=data["customPublicationCode"],
            name=data["name"],
            cover_url=cover_url,
            description=description,
            next_issue_date=_extract_next_issue_date(description),
            categories=[
                Category(id=c["id"], name=c["name"]) for c in data.get("categories", [])
            ],
            issues=[Issue.from_api(i) for i in data.get("issues", [])],
        )
