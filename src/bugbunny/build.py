"""Immutable identity for the exact BugBunny implementation in this process."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from bugbunny import __version__

REVIEW_SCHEMA_VERSION = "bugbunny-review-v3"
BENCHMARK_PLAN_SCHEMA_VERSION = "bugbunny-benchmark-plan-v2"
BENCHMARK_RUN_SCHEMA_VERSION = "bugbunny-benchmark-run-v2"
EXPORT_MANIFEST_SCHEMA_VERSION = "bugbunny-codereviewbench-export-v2"
EXPORT_INDEX_SCHEMA_VERSION = "bugbunny-codereviewbench-export-index-v3"
CANDIDATE_AUDIT_SCHEMA_VERSION = "bugbunny-candidate-audit-v2"
EVALUATION_AUDIT_SCHEMA_VERSION = "bugbunny-evaluation-audit-v3"
IMPLEMENTATION_IDENTITY_SCHEMA = "bugbunny-implementation-v1"


@lru_cache(maxsize=1)
def _source_identity() -> tuple[str, int]:
    """Hash the installed package once without exposing mutable cached state."""

    package_root = Path(__file__).resolve().parent
    source_files = sorted(
        package_root.rglob("*.py"),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in source_files:
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest(), len(source_files)


def implementation_identity() -> dict[str, Any]:
    """Return a path-independent identity of all BugBunny source files.

    Package versions are release labels, not build identities: editable installs
    and unreleased commits can share a version while implementing different
    review, validation, or export semantics. Hashing the loaded package source
    makes those differences part of every resumability/evaluation contract.
    """

    source_sha256, source_file_count = _source_identity()
    return {
        "schema_version": IMPLEMENTATION_IDENTITY_SCHEMA,
        "package_version": __version__,
        "source_sha256": source_sha256,
        "source_file_count": source_file_count,
    }


__all__ = [
    "BENCHMARK_PLAN_SCHEMA_VERSION",
    "BENCHMARK_RUN_SCHEMA_VERSION",
    "CANDIDATE_AUDIT_SCHEMA_VERSION",
    "EVALUATION_AUDIT_SCHEMA_VERSION",
    "EXPORT_INDEX_SCHEMA_VERSION",
    "EXPORT_MANIFEST_SCHEMA_VERSION",
    "IMPLEMENTATION_IDENTITY_SCHEMA",
    "REVIEW_SCHEMA_VERSION",
    "implementation_identity",
]
