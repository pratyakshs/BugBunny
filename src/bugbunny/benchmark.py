"""Leak-resistant bridge to CodeReviewBench's offline JSON artifacts.

This module intentionally does not import CodeReviewBench. It consumes the
published ``offline/results/benchmark_data.json`` schema and emits the three
files its judge needs. Golden text is validated and hashed during loading, but
is never retained in an engine-facing case.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bugbunny import __version__
from bugbunny.schemas import CATEGORIES, SEVERITIES
from bugbunny.util import atomic_write_json, canonical_json, sha256_bytes, sha256_text
from bugbunny.validation import artifact_location_is_commentable

STANDARD_CASE_COUNT = 50
_PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
_TOOL_COMPONENT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CodeReviewBenchCase:
    """One offline case with a safe cloned-fixture input.

    ``golden_url`` is a join key for export only. ``review_url`` points at a
    cloned fixture PR and is the only URL exposed by :meth:`to_engine_input`.
    This keeps the original PR's human discussion out of the review engine.
    """

    case_id: str
    golden_url: str
    review_url: str
    repository: str
    pr_number: int
    fixture_tool: str
    fixture_repo_name: str
    golden_sha256: str

    def to_engine_input(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "pr_url": self.review_url,
            "repository": self.repository,
            "pr_number": self.pr_number,
        }


@dataclass(frozen=True)
class CodeReviewBenchManifest:
    schema_version: str
    benchmark_data_path: str
    benchmark_sha256: str
    golden_sha256: str
    case_count: int
    golden_issue_count: int
    preferred_fixture_tool: str
    fixture_tool_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodeReviewBenchDataset:
    cases: tuple[CodeReviewBenchCase, ...]
    manifest: CodeReviewBenchManifest

    def by_golden_url(self) -> dict[str, CodeReviewBenchCase]:
        return {case.golden_url: case for case in self.cases}

    def by_id(self) -> dict[str, CodeReviewBenchCase]:
        return {case.case_id: case for case in self.cases}


@dataclass(frozen=True)
class CodeReviewBenchExport:
    benchmark_data_path: Path
    candidates_path: Path
    dedup_groups_path: Path
    manifest_path: Path
    tool_id: str
    review_count: int
    candidate_count: int
    input_benchmark_sha256: str
    input_golden_sha256: str
    output_golden_sha256: str
    output_files_sha256: dict[str, str]
    manifest_sha256: str


def _validate_pr_url(url: str, *, label: str) -> tuple[str, str, int]:
    match = _PR_URL.fullmatch(url)
    if not match:
        raise ValueError(f"{label} is not a GitHub pull-request URL: {url!r}")
    return match.group(1), match.group(2), int(match.group(3))


def case_id_for_url(url: str) -> str:
    owner, repo, number = _validate_pr_url(url, label="benchmark URL")
    canonical = f"https://github.com/{owner}/{repo}/pull/{number}"
    readable = _TOOL_COMPONENT.sub("-", f"{owner}-{repo}-pr{number}".lower()).strip("-")
    return f"{readable}-{sha256_text(canonical)[:10]}"


def sanitize_model_name(model: str) -> str:
    """Match CodeReviewBench's judge-model directory convention exactly."""

    value = model.strip().replace("/", "_")
    if (
        not value
        or value in {".", ".."}
        or "\x00" in value
        or "\\" in value
        or "\n" in value
        or "\r" in value
        or len(value) > 200
    ):
        raise ValueError("judge model cannot be represented as a safe benchmark directory")
    return value


def artifact_model_directory(model: str) -> str:
    """Return a collision-resistant directory for BugBunny run artifacts."""

    readable = sanitize_model_name(model)
    return f"{readable[:148]}--{sha256_text(model)[:10]}"


def tool_model_id(tool: str, model: str, evaluation_fingerprint: str = "") -> str:
    """Return the deterministic tool/model key stored in benchmark results."""

    tool_part = _TOOL_COMPONENT.sub("-", tool.lower()).strip("-")
    model_part = _TOOL_COMPONENT.sub("-", model.lower()).strip("-")
    if not tool_part or not model_part:
        raise ValueError("tool and model must contain letters or digits")
    value = f"{tool_part}-{model_part}"
    # Preserve a readable prefix while making punctuation/case normalization
    # injective for practical purposes. Hash the original strings, not their
    # lossy normalized representation.
    suffix = sha256_text(f"{tool}\0{model}\0{evaluation_fingerprint}")[:12]
    return f"{value[:83].rstrip('-')}-{suffix}"


def dedicated_fixture_tool(tool: str, model: str) -> str:
    """Return the name to encode in a newly cloned publish fixture.

    Existing organization fixtures may be reused as read-only inputs after
    their SHAs are resolved. They must not be reused for publishing: Step 1
    derives tool identity from the repository name and collects the repository's
    bot comments, so publishing into another tool's fixture misattributes and
    contaminates the run. Published evaluations need one dedicated
    ``__bugbunny-<model>__`` fixture per benchmark case.
    """

    tool_part = _TOOL_COMPONENT.sub("-", tool.lower()).strip("-")
    model_part = _TOOL_COMPONENT.sub("-", model.lower()).strip("-")
    if not tool_part or not model_part:
        raise ValueError("tool and model must contain letters or digits")
    # CodeReviewBench step0 truncates tool slugs to 30 characters. Produce the
    # final <=30-character identity ourselves so the retained hash survives
    # and Step1 attributes published comments to the expected tool.
    suffix = sha256_text(f"{tool}\0{model}")[:8]
    readable = f"{tool_part}-{model_part}"
    return f"{readable[:21].rstrip('-')}-{suffix}"


def _load_json_object(path: Path | str) -> tuple[Path, bytes, dict[str, Any]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"benchmark_data.json does not exist: {source}")
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {source}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {source}")
    return source, raw, value


def _golden_projection(data: Mapping[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for url, entry in sorted(data.items()):
        if not isinstance(entry, Mapping):
            raise ValueError(f"benchmark entry for {url!r} is not an object")
        comments = entry.get("golden_comments")
        if not isinstance(comments, list):
            raise ValueError(f"benchmark entry for {url!r} has no golden_comments array")
        projection[str(url)] = {
            "pr_title": entry.get("pr_title"),
            "source_repo": entry.get("source_repo"),
            "golden_source_file": entry.get("golden_source_file"),
            "golden_comments": deepcopy(comments),
        }
    return projection


def _golden_hash(data: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(_golden_projection(data)))


def _select_fixture_review(
    reviews: Any,
    *,
    preferred_tool: str,
    require_preferred_tool: bool,
) -> tuple[str, str, str]:
    if not isinstance(reviews, list):
        raise ValueError("benchmark reviews must be an array")
    valid: list[tuple[str, str, str]] = []
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        tool = str(review.get("tool") or "").strip()
        pr_url = str(review.get("pr_url") or "").strip()
        repo_name = str(review.get("repo_name") or "").strip()
        if not tool or not pr_url:
            continue
        _owner, parsed_repo, _number = _validate_pr_url(pr_url, label="fixture pr_url")
        valid.append((tool, pr_url, repo_name or parsed_repo))
    if not valid:
        raise ValueError("benchmark case has no usable fixture reviews")

    if preferred_tool != "auto":
        preferred = sorted(item for item in valid if item[0] == preferred_tool)
        if preferred:
            return preferred[0]
        if require_preferred_tool:
            raise ValueError(f"benchmark case has no fixture for tool {preferred_tool!r}")
    return sorted(valid)[0]


def load_codereviewbench_dataset(
    benchmark_data_path: Path | str,
    *,
    preferred_fixture_tool: str = "auto",
    expected_case_count: int | None = None,
    require_preferred_tool: bool = False,
) -> CodeReviewBenchDataset:
    """Load CodeReviewBench's existing ``benchmark_data.json``.

    Golden comments are validated and hashed then discarded. Set
    ``expected_case_count=50`` to require complete standard-suite coverage.
    A requested fixture tool is preferred for every case. The default ``auto``
    policy deterministically selects the first valid fixture for each case.
    """

    if not preferred_fixture_tool.strip():
        raise ValueError("preferred_fixture_tool must not be empty")
    source, raw, data = _load_json_object(benchmark_data_path)
    if expected_case_count is not None and len(data) != expected_case_count:
        raise ValueError(f"expected {expected_case_count} benchmark cases, found {len(data)}")

    cases: list[CodeReviewBenchCase] = []
    fixture_counts: dict[str, int] = {}
    seen_fixture_urls: set[str] = set()
    golden_issue_count = 0
    for golden_url, entry in sorted(data.items()):
        if not isinstance(golden_url, str) or not isinstance(entry, Mapping):
            raise ValueError("benchmark_data.json must map URL strings to objects")
        owner, repo, number = _validate_pr_url(golden_url, label="golden URL")
        comments = entry.get("golden_comments")
        if not isinstance(comments, list) or not comments:
            raise ValueError(f"benchmark case {golden_url} has no golden comments")
        for index, comment in enumerate(comments):
            if (
                not isinstance(comment, Mapping)
                or not isinstance(comment.get("comment"), str)
                or not str(comment["comment"]).strip()
            ):
                raise ValueError(f"benchmark case {golden_url} golden_comments[{index}] is invalid")
        fixture_tool, review_url, fixture_repo_name = _select_fixture_review(
            entry.get("reviews"),
            preferred_tool=preferred_fixture_tool,
            require_preferred_tool=require_preferred_tool,
        )
        if review_url in seen_fixture_urls:
            raise ValueError(f"fixture PR is selected for multiple cases: {review_url}")
        seen_fixture_urls.add(review_url)
        fixture_counts[fixture_tool] = fixture_counts.get(fixture_tool, 0) + 1
        golden_issue_count += len(comments)
        cases.append(
            CodeReviewBenchCase(
                case_id=case_id_for_url(golden_url),
                golden_url=golden_url,
                review_url=review_url,
                repository=f"{owner}/{repo}",
                pr_number=number,
                fixture_tool=fixture_tool,
                fixture_repo_name=fixture_repo_name,
                golden_sha256=sha256_text(
                    canonical_json({"golden_url": golden_url, "golden_comments": comments})
                ),
            )
        )

    return CodeReviewBenchDataset(
        cases=tuple(cases),
        manifest=CodeReviewBenchManifest(
            schema_version="bugbunny-codereviewbench-dataset-v1",
            benchmark_data_path=source.name,
            benchmark_sha256=sha256_bytes(raw),
            golden_sha256=_golden_hash(data),
            case_count=len(cases),
            golden_issue_count=golden_issue_count,
            preferred_fixture_tool=preferred_fixture_tool,
            fixture_tool_counts=dict(sorted(fixture_counts.items())),
        ),
    )


load_benchmark_data = load_codereviewbench_dataset


def _artifact_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
        raise TypeError("review artifact to_dict() must return a mapping")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"expected a ReviewArtifact or mapping, got {type(value).__name__}")


def _artifact_model(artifact: Mapping[str, Any]) -> str | None:
    config = artifact.get("config")
    if isinstance(config, Mapping) and config.get("model"):
        return str(config["model"])
    return str(artifact["model"]) if artifact.get("model") else None


def _artifact_golden_url(
    artifact: Mapping[str, Any],
    *,
    supplied_key: str | None,
    benchmark_data: Mapping[str, Any],
) -> str:
    benchmark = artifact.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise ValueError("export requires CodeReviewBench provenance metadata")
    value = benchmark.get("golden_url")
    if not isinstance(value, str) or value not in benchmark_data:
        raise KeyError("artifact.benchmark.golden_url is not present in benchmark data")
    if supplied_key is not None and supplied_key != value:
        raise ValueError("artifact mapping key does not match benchmark.golden_url")
    return value


def _normalize_artifacts(
    artifacts: Mapping[str, Any] | Iterable[Any],
    benchmark_data: Mapping[str, Any],
    *,
    benchmark_sha256: str,
    golden_sha256: str,
) -> dict[str, dict[str, Any]]:
    if isinstance(artifacts, Mapping) and "findings" not in artifacts:
        values = [(str(key), raw) for key, raw in artifacts.items()]
    elif isinstance(artifacts, Mapping):
        values = [(None, artifacts)]
    else:
        values = [(None, raw) for raw in artifacts]
    normalized: dict[str, dict[str, Any]] = {}
    for supplied_key, raw in values:
        artifact = _artifact_mapping(raw)
        if artifact.get("schema_version") != "bugbunny-review-v1":
            raise ValueError("only native BugBunny ReviewArtifacts can be exported")
        if artifact.get("tool") != "bugbunny" or artifact.get("tool_version") != __version__:
            raise ValueError("artifact tool identity/version does not match this BugBunny build")
        if artifact.get("status") != "completed":
            raise ValueError("only completed ReviewArtifacts can be exported")
        golden_url = _artifact_golden_url(
            artifact,
            supplied_key=supplied_key,
            benchmark_data=benchmark_data,
        )
        if golden_url in normalized:
            raise ValueError(f"multiple review artifacts target {golden_url}")
        entry = benchmark_data[golden_url]
        if not isinstance(entry, Mapping):
            raise ValueError(f"benchmark entry for {golden_url} is not an object")
        benchmark = artifact["benchmark"]
        if benchmark.get("suite") != "CodeReviewBench":
            raise ValueError("artifact is not marked as a CodeReviewBench run")
        expected_case_hash = sha256_text(
            canonical_json(
                {
                    "golden_url": golden_url,
                    "golden_comments": entry.get("golden_comments"),
                }
            )
        )
        for name, expected in (
            ("benchmark_sha256", benchmark_sha256),
            ("dataset_golden_sha256", golden_sha256),
            ("golden_sha256", expected_case_hash),
        ):
            if benchmark.get(name) != expected:
                raise ValueError(f"artifact {name} does not match the supplied dataset")
        if benchmark.get("case_id") != case_id_for_url(golden_url):
            raise ValueError("artifact case_id does not match its golden URL")
        review_url = benchmark.get("review_url")
        fixture_tool = benchmark.get("fixture_tool")
        valid_reviews = {
            (str(review.get("pr_url")), str(review.get("tool")))
            for review in entry.get("reviews", [])
            if isinstance(review, Mapping) and review.get("pr_url") and review.get("tool")
        }
        if (str(review_url), str(fixture_tool)) not in valid_reviews:
            raise ValueError("artifact fixture URL/tool is not present in the benchmark case")
        pr = artifact.get("pr")
        if not isinstance(pr, Mapping) or pr.get("url") != review_url:
            raise ValueError("artifact PR URL does not match its benchmark fixture URL")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(pr.get("base_sha") or "")):
            raise ValueError("artifact lacks an exact base SHA")
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", str(pr.get("head_sha") or "")):
            raise ValueError("artifact lacks an exact head SHA")
        coverage = artifact.get("coverage")
        diff = artifact.get("diff")
        if not isinstance(coverage, Mapping) or coverage.get("complete") is not True:
            raise ValueError("only artifacts with complete diff coverage can be exported")
        if not isinstance(diff, Mapping) or diff.get("chunk_plan_complete") is not True:
            raise ValueError("artifact does not prove a complete chunk plan")
        if not isinstance(artifact.get("config"), Mapping):
            raise ValueError("artifact lacks review configuration")
        if not isinstance(artifact.get("context"), Mapping):
            raise ValueError("artifact lacks prompt/context provenance")
        if not isinstance(artifact.get("runtime"), Mapping):
            raise ValueError("artifact lacks gateway/runtime provenance")
        if not isinstance(artifact.get("findings", []), list):
            raise ValueError(f"review artifact for {golden_url} has no findings array")
        normalized[golden_url] = artifact
    return normalized


def _artifact_review_url(
    golden_url: str,
    artifact: Mapping[str, Any],
    benchmark_entry: Mapping[str, Any],
) -> str:
    del benchmark_entry
    benchmark = artifact["benchmark"]
    value = str(benchmark["review_url"])
    _validate_pr_url(value, label="artifact fixture URL")
    if value == golden_url:
        raise ValueError("artifact fixture URL must not be the golden PR URL")
    return value


def _finding_sort_key(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(finding.get("path") or ""),
        int(finding.get("line") or finding.get("start_line") or 0),
        int(finding.get("end_line") or finding.get("line") or 0),
        str(finding.get("finding_id") or finding.get("fingerprint") or ""),
        str(finding.get("title") or ""),
    )


def _render_candidate_text(finding: Mapping[str, Any]) -> str:
    title = str(finding.get("title") or "Code issue").strip()
    body = str(finding.get("body") or "").strip()
    trigger = str(finding.get("trigger") or "").strip()
    impact = str(finding.get("impact") or "").strip()
    evidence_value = finding.get("evidence")
    evidence = evidence_value.strip() if isinstance(evidence_value, str) else ""
    fix = str(finding.get("suggested_fix") or "").strip()
    path = str(finding.get("path") or "unknown")
    line = int(finding.get("line") or finding.get("start_line") or 0)
    end_line = int(finding.get("end_line") or line)
    side = str(finding.get("side") or "RIGHT").upper()
    location = f"{path}:{line}" + (f"-{end_line}" if end_line != line else "")
    sections = [f"Location: {location} ({side})", title]
    for label, value in (
        ("", body if not trigger and not impact else ""),
        ("Trigger", trigger),
        ("Impact", impact),
        ("Evidence", evidence if evidence not in body else ""),
        ("Suggested fix", fix),
    ):
        if value:
            sections.append(f"{label}: {value}" if label else value)
    return "\n\n".join(sections)


def _direct_outputs(
    artifact: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_findings = artifact.get("findings", [])
    ordered = sorted(
        (dict(value) for value in raw_findings if isinstance(value, Mapping)),
        key=_finding_sort_key,
    )
    if len(ordered) != len(raw_findings):
        raise ValueError("every final finding must be an object")
    completed_at = str(
        artifact.get("completed_at") or artifact.get("started_at") or "1970-01-01T00:00:00Z"
    )
    comments: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, int | None, str]] = set()
    diff = artifact.get("diff")
    for index, finding in enumerate(ordered):
        raw_path = finding.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"final finding {index} has an empty or non-string path")
        path = raw_path.strip()
        parsed_path = PurePosixPath(path)
        if (
            parsed_path.is_absolute()
            or ".." in parsed_path.parts
            or "." in parsed_path.parts
            or str(parsed_path) != path
            or "\\" in path
            or any(character in path for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError(f"final finding {index} has an unsafe path")
        line = finding.get("line")
        end_line = finding.get("end_line")
        if (
            not isinstance(line, int)
            or isinstance(line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or line <= 0
            or end_line < line
        ):
            raise ValueError(f"final finding {index} has an invalid line range")
        side = finding.get("side")
        if side not in {"RIGHT", "LEFT"}:
            raise ValueError(f"final finding {index} has an invalid diff side")
        if not artifact_location_is_commentable(
            diff,
            path=path,
            side=side,
            line=line,
            end_line=end_line,
        ):
            raise ValueError(f"final finding {index} is outside the changed-line ledger")
        finding_id = finding.get("finding_id")
        fingerprint = finding.get("fingerprint")
        if not isinstance(finding_id, str) or not re.fullmatch(r"bb-[0-9a-f]{20}", finding_id):
            raise ValueError(f"final finding {index} has an invalid BugBunny finding ID")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"final finding {index} has an invalid semantic fingerprint")
        for field in (
            "title",
            "body",
            "trigger",
            "impact",
            "evidence",
            "suggested_fix",
            "chunk_id",
        ):
            if not isinstance(finding.get(field), str) or not str(finding[field]).strip():
                raise ValueError(f"final finding {index} has an empty {field}")
        if finding.get("severity") not in SEVERITIES or finding.get("category") not in CATEGORIES:
            raise ValueError(f"final finding {index} has an invalid severity or category")
        confidence = finding.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError(f"final finding {index} has an invalid confidence")
        text = _render_candidate_text(finding)
        if not text.strip():
            raise ValueError(f"final finding {index} has no candidate text")
        identity = (finding_id, path, line, text)
        if identity in seen:
            raise ValueError(f"final finding {index} duplicates another final finding")
        seen.add(identity)
        comments.append({"path": path, "line": line, "body": text, "created_at": completed_at})
        candidates.append({"text": text, "path": path, "line": line, "source": "direct"})
    return comments, candidates


def _read_optional_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in existing export {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"existing export is not a JSON object: {path}")
    return value


def _export_output_paths(
    results_root: Path,
    judge_model_directory: str,
) -> dict[str, Path]:
    """Return the three physical files consumed by CodeReviewBench Step 3."""

    return {
        "benchmark_data.json": results_root / "benchmark_data.json",
        f"{judge_model_directory}/candidates.json": (
            results_root / judge_model_directory / "candidates.json"
        ),
        f"{judge_model_directory}/dedup_groups.json": (
            results_root / judge_model_directory / "dedup_groups.json"
        ),
    }


def _hash_export_outputs(paths: Mapping[str, Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative, path in paths.items():
        if not path.is_file():
            raise ValueError(f"export output does not exist: {path}")
        hashes[relative] = sha256_bytes(path.read_bytes())
    return dict(sorted(hashes.items()))


def _refresh_prior_export_manifests(
    judge_dir: Path,
    *,
    current_manifest: Path,
    output_files_sha256: Mapping[str, str],
) -> None:
    """Bind prior per-model manifests to the current shared judge bundle.

    CodeReviewBench stores every review model in one ``benchmark_data.json`` and
    one judge-model candidates/dedup pair. Adding a model therefore changes the
    physical files referenced by earlier model manifests. Refresh only native
    BugBunny export manifests after the three shared files have committed; the
    current model's manifest is written last by the caller.
    """

    if not judge_dir.is_dir():
        return
    for path in sorted(judge_dir.glob("*_export_manifest.json")):
        if path == current_manifest:
            continue
        value = _read_optional_object(path)
        if value.get("schema_version") != "bugbunny-codereviewbench-export-v1":
            continue
        if value.get("judge_model_directory") != judge_dir.name:
            raise ValueError(f"existing export manifest has the wrong judge directory: {path}")
        value["output_files_sha256"] = dict(sorted(output_files_sha256.items()))
        atomic_write_json(path, value)


def verify_codereviewbench_export_manifest(manifest_path: Path | str) -> dict[str, Any]:
    """Verify one committed BugBunny export bundle without invoking a judge.

    The manifest lives below ``<results>/<judge-model>/``. Verification binds
    the exact three Step 3 input files, checks that their golden projection is
    unchanged, and proves that this tool has one consistent review/candidate/
    singleton-group population across those files. A mismatch raises
    :class:`ValueError`; the returned report is safe to record or print.
    """

    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"export manifest does not exist: {path}")
    manifest = _read_optional_object(path)
    if manifest.get("schema_version") != "bugbunny-codereviewbench-export-v1":
        raise ValueError("unsupported CodeReviewBench export manifest")
    tool_id = manifest.get("tool_id")
    judge_directory = manifest.get("judge_model_directory")
    if not isinstance(tool_id, str) or not tool_id:
        raise ValueError("export manifest has no tool_id")
    if (
        not isinstance(judge_directory, str)
        or sanitize_model_name(str(manifest.get("judge_model") or "")) != judge_directory
    ):
        raise ValueError("export manifest judge model/directory do not match")
    if path.parent.name != judge_directory or path.name != f"{tool_id}_export_manifest.json":
        raise ValueError("export manifest is not at its declared results location")

    results_root = path.parent.parent.resolve()
    output_paths = _export_output_paths(results_root, judge_directory)
    expected_hashes = manifest.get("output_files_sha256")
    if not isinstance(expected_hashes, Mapping) or set(expected_hashes) != set(output_paths):
        raise ValueError("export manifest does not bind all Step 3 output files")
    actual_hashes = _hash_export_outputs(output_paths)
    if dict(expected_hashes) != actual_hashes:
        raise ValueError("one or more Step 3 output files do not match the export manifest")

    benchmark_data = _read_optional_object(output_paths["benchmark_data.json"])
    candidates = _read_optional_object(output_paths[f"{judge_directory}/candidates.json"])
    groups = _read_optional_object(output_paths[f"{judge_directory}/dedup_groups.json"])
    golden_hash = _golden_hash(benchmark_data)
    if golden_hash != manifest.get("input_golden_sha256") or golden_hash != manifest.get(
        "output_golden_sha256"
    ):
        raise ValueError("exported golden fields do not match the export manifest")

    review_urls: set[str] = set()
    candidate_urls = {
        str(golden_url)
        for golden_url, per_case in candidates.items()
        if isinstance(per_case, Mapping) and tool_id in per_case
    }
    group_urls = {
        str(golden_url)
        for golden_url, per_case in groups.items()
        if isinstance(per_case, Mapping) and tool_id in per_case
    }
    candidate_count = 0
    for golden_url, entry in benchmark_data.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"benchmark entry for {golden_url!r} is not an object")
        matching_reviews = [
            review
            for review in entry.get("reviews", [])
            if isinstance(review, Mapping) and review.get("tool") == tool_id
        ]
        if len(matching_reviews) > 1:
            raise ValueError(f"export contains duplicate {tool_id} reviews for {golden_url}")
        if matching_reviews:
            review_urls.add(golden_url)

        per_case_candidates = candidates.get(golden_url)
        direct = (
            per_case_candidates.get(tool_id) if isinstance(per_case_candidates, Mapping) else None
        )
        if direct is not None:
            if not isinstance(direct, list):
                raise ValueError(f"export candidates for {golden_url} are not an array")
            if any(
                not isinstance(candidate, Mapping)
                or not isinstance(candidate.get("text"), str)
                or not str(candidate["text"]).strip()
                for candidate in direct
            ):
                raise ValueError(f"export candidates for {golden_url} are malformed")
            candidate_count += len(direct)

        per_case_groups = groups.get(golden_url)
        direct_groups = (
            per_case_groups.get(tool_id) if isinstance(per_case_groups, Mapping) else None
        )
        if direct_groups is not None and not isinstance(direct_groups, list):
            raise ValueError(f"export dedup groups for {golden_url} are not an array")

        if matching_reviews:
            if direct is None or direct_groups is None:
                raise ValueError(f"export is missing candidates or groups for {golden_url}")
            expected_groups = [[index] for index in range(len(direct))]
            if direct_groups != expected_groups:
                raise ValueError(
                    f"export singleton groups do not match candidates for {golden_url}"
                )
            comments = matching_reviews[0].get("review_comments")
            if not isinstance(comments, list) or len(comments) != len(direct):
                raise ValueError(f"export review comments do not match candidates for {golden_url}")
            if any(
                not isinstance(comment, Mapping) or comment.get("body") != direct[index].get("text")
                for index, comment in enumerate(comments)
            ):
                raise ValueError(f"export review/candidate text differs for {golden_url}")

    if review_urls != candidate_urls or review_urls != group_urls:
        raise ValueError("export tool populations differ across Step 3 files")
    if len(review_urls) != manifest.get("review_count"):
        raise ValueError("export review_count does not match the Step 3 files")
    if candidate_count != manifest.get("candidate_count"):
        raise ValueError("export candidate_count does not match the Step 3 files")
    artifact_hashes = manifest.get("artifact_canonical_sha256")
    if not isinstance(artifact_hashes, Mapping) or set(artifact_hashes) != review_urls:
        raise ValueError("export artifact population does not match the Step 3 files")

    return {
        "ok": True,
        "manifest": str(path),
        "manifest_sha256": sha256_bytes(path.read_bytes()),
        "tool_id": tool_id,
        "review_count": len(review_urls),
        "candidate_count": candidate_count,
        "output_files_sha256": actual_hashes,
    }


def export_codereviewbench_results(
    base_benchmark_data_path: Path | str,
    artifacts: Mapping[str, Any] | Iterable[Any],
    *,
    output_dir: Path | str,
    judge_model: str,
    tool: str = "bugbunny",
    review_model: str | None = None,
    expected_case_count: int | None = None,
) -> CodeReviewBenchExport:
    """Export final findings directly into CodeReviewBench's expected schemas.

    ``output_dir`` is CodeReviewBench's ``results`` directory. The copied
    benchmark is written to ``benchmark_data.json`` and direct candidates plus
    singleton dedup groups go under ``<sanitized-judge-model>/``. Existing
    tools are preserved and the deterministic BugBunny tool/model entry is
    replaced for each supplied case.
    """

    source, raw, source_data = _load_json_object(base_benchmark_data_path)
    if expected_case_count is not None and len(source_data) != expected_case_count:
        raise ValueError(
            f"expected {expected_case_count} benchmark cases, found {len(source_data)}"
        )
    input_benchmark_sha256 = sha256_bytes(raw)
    input_golden_sha256 = _golden_hash(source_data)
    normalized = _normalize_artifacts(
        artifacts,
        source_data,
        benchmark_sha256=input_benchmark_sha256,
        golden_sha256=input_golden_sha256,
    )

    inferred_models = {
        model for artifact in normalized.values() if (model := _artifact_model(artifact))
    }
    if review_model is None:
        if len(inferred_models) != 1:
            raise ValueError(
                "review_model is required when artifacts do not identify exactly one model"
            )
        review_model = next(iter(inferred_models))
    elif inferred_models and inferred_models != {review_model}:
        raise ValueError(
            f"artifact models {sorted(inferred_models)!r} do not match {review_model!r}"
        )
    projections = {
        canonical_json(
            {
                "schema_version": artifact.get("schema_version"),
                "tool_version": artifact.get("tool_version"),
                "config": artifact.get("config"),
                "context": {
                    key: artifact.get("context", {}).get(key)
                    for key in (
                        "generation_prompt_version",
                        "generation_prompt_sha256",
                        "verifier_prompt_version",
                        "verifier_prompt_sha256",
                        "context_selection_prompt_version",
                        "context_selection_prompt_sha256",
                        "context_selection_schema_version",
                        "context_selection_schema_sha256",
                    )
                },
                "runtime": artifact.get("runtime"),
            }
        )
        for artifact in normalized.values()
    }
    if len(projections) != 1:
        raise ValueError("all artifacts in one export must share one evaluation configuration")
    evaluation_fingerprint = sha256_text(next(iter(projections)))
    tool_id = tool_model_id(tool, review_model, evaluation_fingerprint)

    results_root = Path(output_dir).expanduser().resolve()
    judge_dir = results_root / sanitize_model_name(judge_model)
    benchmark_output = results_root / "benchmark_data.json"
    candidates_output = judge_dir / "candidates.json"
    dedup_output = judge_dir / "dedup_groups.json"
    manifest_output = judge_dir / f"{tool_id}_export_manifest.json"

    benchmark_data: dict[str, Any] = deepcopy(source_data)
    if benchmark_output.is_file():
        _existing_path, _existing_raw, existing_data = _load_json_object(benchmark_output)
        if _golden_hash(existing_data) != input_golden_sha256:
            raise ValueError("existing export contains a different golden dataset")
        for url, existing_entry in existing_data.items():
            target_entry = benchmark_data.get(url)
            if not isinstance(existing_entry, Mapping) or not isinstance(target_entry, dict):
                continue
            prior = [
                deepcopy(review)
                for review in existing_entry.get("reviews", [])
                if isinstance(review, Mapping)
                and str(review.get("tool") or "").startswith(f"{tool}-")
            ]
            target_reviews = target_entry.get("reviews", [])
            if isinstance(target_reviews, list):
                existing_tools = {
                    str(review.get("tool"))
                    for review in target_reviews
                    if isinstance(review, Mapping)
                }
                target_entry["reviews"] = target_reviews + [
                    review for review in prior if str(review.get("tool")) not in existing_tools
                ]
    candidates = _read_optional_object(candidates_output)
    dedup_groups = _read_optional_object(dedup_output)
    # A repeated or subset export for the same evaluation identity replaces
    # that identity atomically across the dataset. Without this global purge,
    # stale cases from an earlier larger run would be silently judged too.
    for entry in benchmark_data.values():
        if isinstance(entry, dict) and isinstance(entry.get("reviews"), list):
            entry["reviews"] = [
                review
                for review in entry["reviews"]
                if not isinstance(review, Mapping) or review.get("tool") != tool_id
            ]
    for per_case in candidates.values():
        if isinstance(per_case, dict):
            per_case.pop(tool_id, None)
    for per_case in dedup_groups.values():
        if isinstance(per_case, dict):
            per_case.pop(tool_id, None)
    candidate_count = 0
    artifact_hashes: dict[str, str] = {}
    for golden_url, artifact in sorted(normalized.items()):
        entry = benchmark_data[golden_url]
        if not isinstance(entry, dict):
            raise ValueError(f"benchmark entry for {golden_url} is not an object")
        fixture_url = _artifact_review_url(golden_url, artifact, entry)
        _fixture_owner, fixture_repo_name, _fixture_number = _validate_pr_url(
            fixture_url, label="fixture URL"
        )
        comments, direct_candidates = _direct_outputs(artifact)
        candidate_count += len(direct_candidates)
        artifact_hashes[golden_url] = hashlib.sha256(
            canonical_json(artifact).encode("utf-8")
        ).hexdigest()

        reviews = entry.setdefault("reviews", [])
        if not isinstance(reviews, list):
            raise ValueError(f"benchmark reviews for {golden_url} is not an array")
        entry["reviews"] = [
            deepcopy(review)
            for review in reviews
            if not isinstance(review, Mapping) or review.get("tool") != tool_id
        ] + [
            {
                "tool": tool_id,
                "repo_name": fixture_repo_name,
                "pr_url": fixture_url,
                "review_comments": comments,
            }
        ]

        candidate_tools = candidates.setdefault(golden_url, {})
        if not isinstance(candidate_tools, dict):
            raise ValueError(f"candidates for {golden_url} is not an object")
        candidate_tools[tool_id] = direct_candidates
        group_tools = dedup_groups.setdefault(golden_url, {})
        if not isinstance(group_tools, dict):
            raise ValueError(f"dedup groups for {golden_url} is not an object")
        group_tools[tool_id] = [[index] for index in range(len(direct_candidates))]

    output_golden_sha256 = _golden_hash(benchmark_data)
    if output_golden_sha256 != input_golden_sha256:
        raise AssertionError("export changed golden benchmark fields")
    atomic_write_json(benchmark_output, benchmark_data)
    atomic_write_json(candidates_output, candidates)
    atomic_write_json(dedup_output, dedup_groups)
    output_files_sha256 = _hash_export_outputs(_export_output_paths(results_root, judge_dir.name))
    _refresh_prior_export_manifests(
        judge_dir,
        current_manifest=manifest_output,
        output_files_sha256=output_files_sha256,
    )
    manifest = {
        "schema_version": "bugbunny-codereviewbench-export-v1",
        "tool": tool,
        "tool_id": tool_id,
        "review_model": review_model,
        "evaluation_fingerprint": evaluation_fingerprint,
        "judge_model": judge_model,
        "judge_model_directory": sanitize_model_name(judge_model),
        "source_benchmark_data_path": source.name,
        "input_benchmark_sha256": input_benchmark_sha256,
        "input_golden_sha256": input_golden_sha256,
        "output_golden_sha256": output_golden_sha256,
        "review_count": len(normalized),
        "candidate_count": candidate_count,
        "candidate_extraction_bypassed": True,
        "deduplication": "singleton-final-findings",
        # Export accepts in-memory mappings as well as files, so this hashes the
        # canonical JSON value. Run manifests separately bind exact file bytes.
        "artifact_canonical_sha256": dict(sorted(artifact_hashes.items())),
        "output_files_sha256": output_files_sha256,
    }
    # This is the commit point for the three already-atomically-replaced judge
    # inputs. A verifier must reject absent or stale manifests after interruption.
    atomic_write_json(manifest_output, manifest)
    manifest_sha256 = sha256_bytes(manifest_output.read_bytes())
    return CodeReviewBenchExport(
        benchmark_data_path=benchmark_output,
        candidates_path=candidates_output,
        dedup_groups_path=dedup_output,
        manifest_path=manifest_output,
        tool_id=tool_id,
        review_count=len(normalized),
        candidate_count=candidate_count,
        input_benchmark_sha256=input_benchmark_sha256,
        input_golden_sha256=input_golden_sha256,
        output_golden_sha256=output_golden_sha256,
        output_files_sha256=output_files_sha256,
        manifest_sha256=manifest_sha256,
    )


export_review_artifacts = export_codereviewbench_results

__all__ = [
    "STANDARD_CASE_COUNT",
    "CodeReviewBenchCase",
    "CodeReviewBenchDataset",
    "CodeReviewBenchExport",
    "CodeReviewBenchManifest",
    "artifact_model_directory",
    "case_id_for_url",
    "dedicated_fixture_tool",
    "export_codereviewbench_results",
    "export_review_artifacts",
    "load_benchmark_data",
    "load_codereviewbench_dataset",
    "sanitize_model_name",
    "tool_model_id",
    "verify_codereviewbench_export_manifest",
]
