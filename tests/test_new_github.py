from __future__ import annotations

import json

import httpx
import pytest

from bugbunny.github import (
    GitHubClient,
    GitHubPermissionError,
    GitHubReviewPublisher,
    parse_pr_url,
)
from bugbunny.models import PRInfo


def _pr() -> PRInfo:
    return PRInfo(
        url="https://github.com/acme/widget/pull/7",
        owner="acme",
        repo="widget",
        number=7,
        clone_url="https://github.com/acme/widget.git",
        title="Fix cleanup",
        body="",
        base_ref="main",
        base_sha="a" * 40,
        head_ref="fix",
        head_sha="b" * 40,
        resolved_at="2026-08-21T00:00:00Z",
    )


def _artifact(*, findings=None) -> dict:
    values = list(findings or [])
    right_ranges: dict[str, list[list[int]]] = {}
    left_ranges: dict[str, list[list[int]]] = {}
    for finding in values:
        target = right_ranges if finding.get("side", "RIGHT") == "RIGHT" else left_ranges
        target.setdefault(finding["path"], []).append([finding.get("line", 0)] * 2)
    return {
        "schema_version": "bugbunny-review-v1",
        "tool": "bugbunny",
        "tool_version": "0.3.0",
        "status": "completed",
        "pr": _pr().to_dict(),
        "config": {"model": "openai/gpt-5.6-luna"},
        "coverage": {"complete": True},
        "diff": {
            "chunk_plan_complete": True,
            "commentable_ranges": {"RIGHT": right_ranges, "LEFT": left_ranges},
        },
        "findings": values,
    }


def _finding(line: int = 12, *, finding_id: str = "f-1") -> dict:
    return {
        "finding_id": finding_id,
        "title": "Promise is discarded",
        "body": "forEach does not await this async callback.",
        "severity": "high",
        "path": "src/cleanup.ts",
        "side": "RIGHT",
        "line": line,
        "end_line": line,
        "evidence": "items.forEach(async item => cleanup(item))",
        "suggested_fix": "Await Promise.all over map.",
    }


def test_resolver_uses_exact_base_and_head_and_retries_get() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/repos/acme/widget/pulls/7"
        assert request.headers["authorization"] == "Bearer private-token"
        if calls == 1:
            return httpx.Response(503, json={"message": "try later"})
        return httpx.Response(
            200,
            json={
                "title": "Fix cleanup",
                "body": None,
                "base": {
                    "ref": "main",
                    "sha": "A" * 40,
                    "repo": {"clone_url": "https://github.com/acme/widget.git"},
                },
                "head": {"ref": "fix", "sha": "B" * 40},
            },
        )

    transport_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    client = GitHubClient(
        token="private-token",
        retries=2,
        sleep=lambda _seconds: None,
        client=transport_client,
    )
    info = client.resolve_pr("https://github.com/acme/widget/pull/7")

    assert calls == 2
    assert info.base_sha == "a" * 40
    assert info.head_sha == "b" * 40
    assert info.full_name == "acme/widget"
    assert parse_pr_url(info.url) == ("acme", "widget", 7)


def test_atomic_publisher_posts_every_final_finding_once_and_is_idempotent() -> None:
    posted: list[dict] = []
    existing: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=existing)
        assert request.method == "POST"
        payload = json.loads(request.content)
        posted.append(payload)
        review = {"id": 91, "html_url": "https://github.com/acme/widget/pull/7#review-91"}
        existing.append({**review, "body": payload["body"]})
        return httpx.Response(200, json=review)

    transport_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token", client=transport_client))
    artifact = _artifact(findings=[_finding(12), _finding(20, finding_id="f-2")])

    first = publisher.publish(_pr(), artifact)
    second = publisher.publish(_pr(), artifact)

    assert first.status == "published"
    assert second.status == "already_published"
    assert len(posted) == 1
    assert posted[0]["commit_id"] == "b" * 40
    assert posted[0]["event"] == "COMMENT"
    assert len(posted[0]["comments"]) == 2
    assert [item["line"] for item in posted[0]["comments"]] == [12, 20]
    assert first.marker in posted[0]["body"]


def test_clean_review_is_not_published_by_default_and_writes_require_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token", client=transport_client))
    result = publisher.publish(_pr(), _artifact())
    assert result.status == "clean_not_published"
    assert requests == []

    no_token = GitHubReviewPublisher(
        GitHubClient(
            token=None,
            client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://api.github.com"
            ),
        )
    )
    # Isolate the test from an ambient developer token.
    no_token.client.token = None
    with pytest.raises(GitHubPermissionError):
        no_token.publish(_pr(), _artifact(findings=[_finding()]))


def test_model_artifact_cannot_select_a_different_write_target() -> None:
    artifact = _artifact(findings=[_finding()])
    artifact["pr"]["url"] = "https://github.com/attacker/other/pull/99"
    transport_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        base_url="https://api.github.com",
    )
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token", client=transport_client))
    with pytest.raises(ValueError, match="different pull request"):
        publisher.publish(_pr(), artifact)


def test_publisher_rejects_a_stale_artifact_when_only_the_pr_base_advanced() -> None:
    artifact = _artifact(findings=[_finding()])
    current = PRInfo(**{**_pr().to_dict(), "base_sha": "c" * 40})
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token"))

    with pytest.raises(ValueError, match="different base SHA"):
        publisher.publish(current, artifact)


def test_publisher_refuses_borrowed_codereviewbench_fixture() -> None:
    artifact = _artifact(findings=[_finding()])
    artifact["benchmark"] = {
        "suite": "CodeReviewBench",
        "fixture_tool": "sampletool",
    }
    transport_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        base_url="https://api.github.com",
    )
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token", client=transport_client))

    with pytest.raises(ValueError, match="borrowed CodeReviewBench fixture"):
        publisher.publish(_pr(), artifact)


def test_publisher_refuses_borrowed_fixture_even_without_benchmark_metadata() -> None:
    pr = _pr()
    pr = PRInfo(
        **{
            **pr.to_dict(),
            "url": "https://github.com/code-review-benchmark/acme__widget__sampletool__PR7__20260821/pull/1",
            "owner": "code-review-benchmark",
            "repo": "acme__widget__sampletool__PR7__20260821",
        }
    )
    artifact = _artifact(findings=[_finding()])
    artifact["pr"] = pr.to_dict()
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token"))

    with pytest.raises(ValueError, match="borrowed CodeReviewBench fixture"):
        publisher.publish(pr, artifact)


@pytest.mark.parametrize("missing", ["status", "schema_version", "tool_version", "pr"])
def test_publisher_rejects_incomplete_or_non_native_artifacts(missing: str) -> None:
    artifact = _artifact(findings=[_finding()])
    artifact.pop(missing)
    publisher = GitHubReviewPublisher(GitHubClient(token="private-token"))

    with pytest.raises(ValueError):
        publisher.publish(_pr(), artifact)


def test_publisher_rejects_an_untrusted_range_end_outside_the_changed_ledger() -> None:
    artifact = _artifact(findings=[{**_finding(), "end_line": 999_999}])
    with pytest.raises(ValueError, match="changed-line ledger"):
        GitHubReviewPublisher._validate_target(_pr(), artifact)

    left = _artifact(findings=[{**_finding(), "side": "LEFT"}])
    assert GitHubReviewPublisher._validate_target(_pr(), left)[0]["side"] == "LEFT"
