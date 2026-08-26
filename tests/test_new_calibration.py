from __future__ import annotations

import json
from pathlib import Path

import pytest

from bugbunny.calibration import (
    CalibrationError,
    load_calibration_corpus,
    load_operating_point,
    select_operating_point,
)
from bugbunny.prompts import VERIFIER_PROMPT_VERSION, verifier_prompt_sha256
from bugbunny.schemas import VERIFIER_SCHEMA
from bugbunny.util import canonical_json, sha256_text


def test_external_corpus_is_mixed_labeled_and_explicitly_benchmark_free() -> None:
    corpus, digest = load_calibration_corpus(Path("calibration/verifier_corpus.json"))
    assert len(corpus["cases"]) == 20
    assert {case["valid_candidate"] for case in corpus["cases"]} == {False, True}
    assert corpus["provenance"]["contains_codereviewbench"] is False
    assert len(digest) == 64


def test_operating_point_maximizes_recall_inside_precision_floor() -> None:
    observations = [
        {"valid_candidate": True, "decision": "keep", "confidence": 0.95},
        {"valid_candidate": True, "decision": "keep", "confidence": 0.70},
        {"valid_candidate": False, "decision": "keep", "confidence": 0.80},
        {"valid_candidate": False, "decision": "drop", "confidence": 0.99},
    ]
    selected = select_operating_point(observations, minimum_precision=0.80)
    assert selected["selected"]["threshold"] == 0.95
    assert selected["selected"]["precision"] == 1.0
    assert selected["selected"]["recall"] == 0.5


def test_unachievable_precision_floor_fails_instead_of_freezing_recall_zero() -> None:
    # Every real threshold has precision 0.5; the all-reject threshold keeps
    # nothing and must not satisfy the floor through vacuous precision.
    observations = [
        {"valid_candidate": True, "decision": "keep", "confidence": 0.5},
        {"valid_candidate": False, "decision": "keep", "confidence": 0.5},
    ]
    with pytest.raises(CalibrationError, match="no threshold satisfies"):
        select_operating_point(observations, minimum_precision=0.80)


def test_thresholds_use_exact_observed_confidences_without_rounding() -> None:
    fine = 0.9000004
    observations = [
        {"valid_candidate": True, "decision": "keep", "confidence": fine},
        {"valid_candidate": False, "decision": "keep", "confidence": 0.1},
    ]
    selected = select_operating_point(observations, minimum_precision=0.80)
    assert selected["selected"]["threshold"] == fine
    assert selected["selected"]["tp"] == 1
    assert selected["selected"]["fp"] == 0


def _operating_point_file(tmp_path: Path) -> tuple[Path, dict]:
    """Build a fully derived operating-point file the way calibrate_verifier does."""

    observations = [
        {"case_id": "one", "valid_candidate": True, "decision": "keep", "confidence": 0.9},
        {"case_id": "two", "valid_candidate": False, "decision": "keep", "confidence": 0.2},
        {"case_id": "three", "valid_candidate": False, "decision": "drop", "confidence": 0.8},
    ]
    selection = select_operating_point(observations, minimum_precision=0.80)
    observation_sha256 = sha256_text(canonical_json(observations))
    corpus_sha256 = "ab" * 32
    identity = sha256_text(
        canonical_json(
            {
                "corpus_sha256": corpus_sha256,
                "verifier_model": "anthropic/test",
                "reasoning_effort": "low",
                "verifier_prompt_sha256": verifier_prompt_sha256(),
                "observation_sha256": observation_sha256,
                "selection": selection["selected"],
            }
        )
    )
    value = {
        "schema_version": "bugbunny-verifier-operating-point-v1",
        "operating_point_id": f"bugbunny-op-{identity[:16]}",
        "verifier_model": "anthropic/test",
        "reasoning_effort": "low",
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "verifier_prompt_sha256": verifier_prompt_sha256(),
        "verifier_schema_sha256": sha256_text(canonical_json(VERIFIER_SCHEMA)),
        "corpus": {"contains_codereviewbench": False, "sha256": corpus_sha256},
        "observation_sha256": observation_sha256,
        "observations": observations,
        "selection": selection,
        "threshold": selection["selected"]["threshold"],
    }
    path = tmp_path / "operating-point.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def test_operating_point_loader_rejects_observation_tampering(tmp_path: Path) -> None:
    path, value = _operating_point_file(tmp_path)
    loaded, digest = load_operating_point(path)
    assert loaded["threshold"] == 0.9
    assert len(digest) == 64

    value["observations"][0]["confidence"] = 0.1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CalibrationError, match="observations"):
        load_operating_point(path)


def test_operating_point_loader_rejects_threshold_tampering(tmp_path: Path) -> None:
    path, value = _operating_point_file(tmp_path)
    load_operating_point(path)

    # Only the operative number is edited; every stored hash stays valid.
    value["threshold"] = 0.1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CalibrationError, match="does not derive"):
        load_operating_point(path)


def test_operating_point_loader_requires_a_bound_selection(tmp_path: Path) -> None:
    path, value = _operating_point_file(tmp_path)
    del value["selection"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CalibrationError, match="selection"):
        load_operating_point(path)


def test_archived_operating_point_still_derives_and_loads() -> None:
    value, digest = load_operating_point(Path("calibration/opus-4-5-low-v1.json"))
    assert value["operating_point_id"] == "bugbunny-op-43df334528999a9c"
    assert value["threshold"] == 0.92
    assert len(digest) == 64
