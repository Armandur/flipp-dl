"""Domain models for flipp-dl."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    def from_api(cls, data: dict[str, Any]) -> "Issue":
        return cls(
            custom_code=data["customIssueCode"],
            issue_name=data.get("issueName", ""),
            issue_date=data.get("issueDate", ""),
        )


@dataclass
class Publication:
    custom_code: str
    name: str
    categories: list[Category] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def num_issues(self) -> int:
        return len(self.issues)

    def has_category(self, category_id: int) -> bool:
        return any(c.id == category_id for c in self.categories)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Publication":
        return cls(
            custom_code=data["customPublicationCode"],
            name=data["name"],
            categories=[
                Category(id=c["id"], name=c["name"])
                for c in data.get("categories", [])
            ],
            issues=[Issue.from_api(i) for i in data.get("issues", [])],
        )
