from __future__ import annotations

from bugbunny.families import consolidate_semantic_duplicates, group_finding_families
from bugbunny.models import Finding


def _finding(*, finding_id: str, path: str, line: int, root: str, **overrides: object) -> Finding:
    values: dict[str, object] = dict(
        title="Nullable lookup is dereferenced",
        body="A missing record raises.",
        path=path,
        side="RIGHT",
        line=line,
        end_line=line,
        severity="high",
        category="bug",
        confidence=0.9,
        evidence="return match.name",
        root_cause=root,
        failure_mode="A missing record is dereferenced and raises.",
        fix_scope="repeated_pattern",
        trigger="The lookup misses.",
        impact="The request fails.",
        suggested_fix="Handle the missing record.",
        chunk_id="chunk",
        finding_id=finding_id,
        fingerprint=finding_id[-1] * 64,
        verifier_confidence=0.9,
        verifier_family_key="nullable_lookup",
    )
    values.update(overrides)
    return Finding(**values)  # type: ignore[arg-type]


def test_semantic_dedup_only_collapses_same_site_paraphrases() -> None:
    first = _finding(finding_id="bb-" + "1" * 20, path="a.py", line=10, root="Nullable lookup")
    paraphrase = _finding(
        finding_id="bb-" + "2" * 20,
        path="a.py",
        line=10,
        root="The lookup result can be null",
    )
    repeated = _finding(
        finding_id="bb-" + "3" * 20,
        path="b.py",
        line=22,
        root="Nullable lookup",
    )
    kept, rejected = consolidate_semantic_duplicates([first, paraphrase, repeated])
    assert kept == [first, repeated]
    assert rejected[0].stage == "semantic_duplicate"


def test_family_grouping_retains_atomic_locations_and_rejects_label_only_collisions() -> None:
    first = _finding(finding_id="bb-" + "1" * 20, path="a.py", line=10, root="Nullable lookup")
    repeated = _finding(finding_id="bb-" + "2" * 20, path="b.py", line=22, root="Nullable lookup")
    unrelated = _finding(
        finding_id="bb-" + "3" * 20,
        path="c.py",
        line=30,
        root="A retry counter underflows and loops forever",
    )
    families = group_finding_families(item.to_dict() for item in (first, repeated, unrelated))
    assert sorted(len(family.members) for family in families) == [1, 2]
    assert {item["path"] for item in next(f for f in families if len(f.members) == 2).members} == {
        "a.py",
        "b.py",
    }


def test_family_label_alone_never_destroys_a_distinct_verifier_kept_finding() -> None:
    # The verifier explicitly kept both findings (it has a merge decision for
    # true duplicates) and its prompt reuses one family key across
    # independently fixable instances of one causal pattern. Two different
    # defects on the same changed line must both survive consolidation.
    null_deref = _finding(
        finding_id="bb-" + "1" * 20,
        path="a.py",
        line=10,
        root="The query result may be None and is dereferenced",
        category="bug",
        failure_mode="A missing row crashes the request handler",
        trigger="The database returns no row",
        suggested_fix="Check for None before attribute access",
        verifier_family_key="input_handling",
    )
    injection = _finding(
        finding_id="bb-" + "2" * 20,
        path="a.py",
        line=10,
        root="User input is interpolated into SQL unparameterized",
        category="security",
        failure_mode="Crafted input executes arbitrary SQL",
        trigger="An attacker submits a quote character",
        suggested_fix="Use bound query parameters",
        verifier_family_key="input_handling",
    )
    kept, rejected = consolidate_semantic_duplicates([null_deref, injection])
    assert kept == [null_deref, injection]
    assert rejected == []


def test_same_category_family_members_with_disjoint_causes_both_survive() -> None:
    first = _finding(
        finding_id="bb-" + "1" * 20,
        path="a.py",
        line=10,
        root="The iterator is exhausted twice",
        failure_mode="The second pass sees no rows",
        trigger="The caller reuses the generator",
        suggested_fix="Materialize the rows once",
        verifier_family_key="iteration",
    )
    second = _finding(
        finding_id="bb-" + "2" * 20,
        path="a.py",
        line=10,
        root="The sort comparator is inconsistent for equal keys",
        failure_mode="Ordering differs between runs",
        trigger="Two rows share a timestamp",
        suggested_fix="Add a total tiebreaker",
        verifier_family_key="iteration",
    )
    kept, rejected = consolidate_semantic_duplicates([first, second])
    assert kept == [first, second]
    assert rejected == []
