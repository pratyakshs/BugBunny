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


def test_operating_point_loader_rejects_observation_tampering(tmp_path: Path) -> None:
    observations = [
        {
            "case_id": "one",
            "valid_candidate": True,
            "decision": "keep",
            "confidence": 0.9,
        }
    ]
    value = {
        "schema_version": "bugbunny-verifier-operating-point-v1",
        "operating_point_id": "bugbunny-op-test",
        "verifier_model": "anthropic/test",
        "reasoning_effort": "low",
        "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
        "verifier_prompt_sha256": verifier_prompt_sha256(),
        "verifier_schema_sha256": sha256_text(canonical_json(VERIFIER_SCHEMA)),
        "corpus": {"contains_codereviewbench": False},
        "observation_sha256": sha256_text(canonical_json(observations)),
        "observations": observations,
        "threshold": 0.8,
    }
    path = tmp_path / "operating-point.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    loaded, digest = load_operating_point(path)
    assert loaded["threshold"] == 0.8
    assert len(digest) == 64

    value["observations"][0]["confidence"] = 0.1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CalibrationError, match="observations"):
        load_operating_point(path)
