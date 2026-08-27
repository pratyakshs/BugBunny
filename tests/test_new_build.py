from __future__ import annotations

from bugbunny import __version__
from bugbunny.build import (
    IMPLEMENTATION_IDENTITY_SCHEMA,
    implementation_identity,
)


def test_implementation_identity_is_stable_and_not_mutably_cached() -> None:
    first = implementation_identity()
    second = implementation_identity()

    assert first == second
    assert first is not second
    assert first == {
        "schema_version": IMPLEMENTATION_IDENTITY_SCHEMA,
        "package_version": __version__,
        "source_sha256": first["source_sha256"],
        "source_file_count": first["source_file_count"],
    }
    assert len(first["source_sha256"]) == 64
    assert first["source_file_count"] > 0

    first["source_sha256"] = "tampered"
    assert implementation_identity() == second
