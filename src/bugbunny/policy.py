"""Versioned review policies shared by prompts, artifacts, and evaluation.

Policies define *what* qualifies for publication.  They are deliberately
separate from the fast/balanced execution profile, which defines *how* a
candidate is validated.  A model sweep therefore cannot accidentally compare
different targets merely because it uses the benchmark command rather than the
single-PR command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from bugbunny.schemas import CATEGORIES

ReviewPolicyName = Literal["production", "codereviewbench"]


@dataclass(frozen=True)
class ReviewPolicy:
    name: ReviewPolicyName
    version: str
    categories: tuple[str, ...]
    contract: str

    @property
    def sha256(self) -> str:
        payload = {
            "name": self.name,
            "version": self.version,
            "categories": list(self.categories),
            "contract": self.contract,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


PRODUCTION_POLICY = ReviewPolicy(
    name="production",
    version="bugbunny-production-policy-v1",
    categories=(
        "bug",
        "security",
        "concurrency",
        "data",
        "api",
        "performance",
        "test_gap",
        "doc_defect",
    ),
    contract="""Report behaviorally meaningful defects that a developer should fix before
merging. Include correctness, security, concurrency, data integrity, API,
performance, test-behavior, and materially misleading documentation defects.
Exclude naming/style preferences, maintainability-only suggestions, and risks
whose trigger and observable impact cannot be established from the evidence.""",
)


CODEREVIEWBENCH_POLICY = ReviewPolicy(
    name="codereviewbench",
    version="bugbunny-codereviewbench-policy-v1",
    categories=tuple(CATEGORIES),
    contract="""Review the full scope used by CodeReviewBench. Include concrete runtime
defects and also independently actionable test gaps, incorrect or stale
documentation, naming/maintainability defects, compatibility concerns, and
evidence-grounded risks that a code reviewer could reasonably request before
merge. A non-runtime concern must still identify the exact changed site, the
specific violated convention or uncertainty, and a concrete corrective action;
do not emit generic advice, compliments, summaries, or unsupported speculation.""",
)

POLICIES: dict[str, ReviewPolicy] = {
    PRODUCTION_POLICY.name: PRODUCTION_POLICY,
    CODEREVIEWBENCH_POLICY.name: CODEREVIEWBENCH_POLICY,
}


def get_review_policy(name: str) -> ReviewPolicy:
    try:
        return POLICIES[name]
    except KeyError as exc:
        raise ValueError(f"unsupported review policy: {name}") from exc


__all__ = [
    "CODEREVIEWBENCH_POLICY",
    "POLICIES",
    "PRODUCTION_POLICY",
    "ReviewPolicy",
    "ReviewPolicyName",
    "get_review_policy",
]
