from __future__ import annotations

import pytest

from bugbunny.analysis import _artifact_stage_counts, _bootstrap_metrics, _metrics


def test_metrics_materialize_generators_before_multiple_reductions() -> None:
    rows = ({"tp": 2, "fp": 0, "fn": 2} for _ in range(1))
    assert _metrics(rows)["f1"] == 2 / 3


def test_single_case_bootstrap_interval_equals_the_observed_metric() -> None:
    intervals = _bootstrap_metrics(
        [{"tp": 2, "fp": 4, "fn": 2}], samples=20, seed=17_042
    )
    assert intervals["precision"] == pytest.approx([1 / 3, 1 / 3])
    assert intervals["recall"] == pytest.approx([0.5, 0.5])
    assert intervals["f1"] == pytest.approx([0.4, 0.4])


def test_stage_counts_separate_prompt_discovery_and_index_pressure() -> None:
    artifacts = [
        {
            "raw_findings": [],
            "validated_findings": [],
            "findings": [],
            "context": {
                "effective_context_files_exposed_to_model": ["src/a.py"],
                "context_pressure": {
                    "generation_contexts_clipped_to_prompt_budget": 1,
                    "largest_context_budget_utilization": 0.97,
                    "largest_verifier_input_char_budget_utilization": 0.42,
                    "selection_bound_hits": {
                        "round_limit_hit": 1,
                        "repository_index_truncated": 1,
                    },
                },
            },
            "calls": [],
            "diagnostics": [],
        },
        {
            "raw_findings": [],
            "validated_findings": [],
            "findings": [],
            "context": {
                "effective_context_files_exposed_to_model": ["src/b.py", "tests/test_b.py"],
                "context_pressure": {
                    "largest_context_budget_utilization": 0.11,
                    "largest_verifier_input_char_budget_utilization": 0.25,
                    "selection_bound_hits": {"repository_index_truncated": 1},
                },
            },
            "calls": [],
            "diagnostics": [],
        },
    ]

    counts = _artifact_stage_counts(artifacts)

    assert counts["reviews_hitting_prompt_or_evidence_bound"] == 1
    assert counts["reviews_hitting_discovery_bound"] == 1
    assert counts["reviews_with_hierarchical_index_summary"] == 2
    assert counts["discovery_bound_hits_by_reason"]["round_limit_hit"] == 1
    assert counts["generation_budget_utilization_mean"] == pytest.approx(0.54)
    assert counts["generation_budget_utilization_max"] == pytest.approx(0.97)
    assert counts["verifier_budget_utilization_max"] == pytest.approx(0.42)
