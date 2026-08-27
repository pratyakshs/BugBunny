"""Small, deliberately write-constrained GitHub integration.

The review engine only ever needs :class:`~bugbunny.models.PRInfo`. It does
not receive this module's client or token. Publishing is a separate explicit
operation which accepts a completed review artifact and creates one GitHub
review containing all inline comments. This boundary prevents model output
from selecting an API endpoint, HTTP method, commit, or repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from bugbunny import __version__
from bugbunny.build import REVIEW_SCHEMA_VERSION, implementation_identity
from bugbunny.models import PRInfo
from bugbunny.util import atomic_write_json, canonical_json, file_lock, utc_now
from bugbunny.validation import artifact_location_is_commentable

_PR_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"([^/?#]+?)(?:\.git)?/pull/([1-9][0-9]*)(?:[/?#].*)?$"
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_MAX_COMMENT_BYTES = 65_536


class GitHubError(RuntimeError):
    """A safe-to-display GitHub failure which never contains credentials."""


class GitHubNotFoundError(GitHubError):
    pass


class GitHubPermissionError(GitHubError):
    pass


class GitHubRateLimitError(GitHubError):
    pass


class GitHubPublishError(GitHubError):
    pass


@dataclass(frozen=True)
class PublishResult:
    status: str
    marker: str
    finding_count: int
    review_id: int | None = None
    html_url: str | None = None

    @property
    def published(self) -> bool:
        return self.status == "published"

    @property
    def already_published(self) -> bool:
        return self.status == "already_published"


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Parse a canonical public GitHub pull-request URL.

    Only ``https://github.com/{owner}/{repo}/pull/{number}`` is accepted. API
    and clone URLs are intentionally rejected so a caller cannot smuggle an
    arbitrary host into a request.
    """

    match = _PR_URL.fullmatch(url.strip())
    if not match:
        raise ValueError(f"expected a GitHub pull-request URL, got {url!r}")
    owner, repo, number = match.groups()
    repo = repo.removesuffix(".git")
    if not repo or repo in {".", ".."}:
        raise ValueError(f"invalid GitHub repository in {url!r}")
    return owner, repo, int(number)


def canonical_pr_url(url: str) -> str:
    owner, repo, number = parse_pr_url(url)
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def _redact(value: str, secrets: Sequence[str]) -> str:
    safe = value
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe


class GitHubClient:
    """Read-only-by-default GitHub REST client built on :mod:`httpx`.

    Public repositories work without a token. Supplying ``token`` (or setting
    ``GITHUB_TOKEN``/``GH_TOKEN``) raises the rate limit and is required by the
    separate publisher. GET requests retry transient failures. Mutating
    requests never retry blindly because an ambiguous retry could duplicate a
    review.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        base_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries <= 0:
            raise ValueError("retries must be positive")
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.timeout = timeout
        self.retries = retries
        self.base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._owns_client = client is None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BugBunny/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = client or httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )
        # Injected clients (especially MockTransport clients) still receive
        # the required headers without exposing them in diagnostics.
        self._client.headers.update(headers)

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def can_write(self) -> bool:
        return bool(self.token)

    def _endpoint(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("GitHub API path must be an absolute path")
        return f"{self.base_url}{path}"

    def _raise_for_status(self, response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        status = response.status_code
        target = response.request.url.path
        message = f"GitHub API {operation} failed with HTTP {status} for {target}"
        if status == 404:
            raise GitHubNotFoundError(message)
        if status in {401, 403}:
            if status == 403 and response.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubRateLimitError(message)
            raise GitHubPermissionError(message)
        if status == 429:
            raise GitHubRateLimitError(message)
        raise GitHubError(message)

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        retry: bool | None = None,
    ) -> Any:
        """Issue one constrained GitHub request and return decoded JSON.

        ``retry`` defaults to true only for GET. Errors intentionally exclude
        response bodies and request headers so a malicious or buggy server
        cannot make a credential appear in logs.
        """

        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported GitHub method: {method}")
        if method == "POST" and not self.token:
            raise GitHubPermissionError("a GitHub token is required to publish a review")
        should_retry = method == "GET" if retry is None else retry
        attempts = self.retries if should_retry else 1
        url = self._endpoint(path)
        last_error: BaseException | None = None

        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=dict(params or {}),
                    json=dict(json_body) if json_body is not None else None,
                    timeout=self.timeout,
                )
                if (
                    should_retry
                    and response.status_code in _TRANSIENT_STATUS
                    and attempt + 1 < attempts
                ):
                    self._sleep(min(2**attempt, 4))
                    continue
                self._raise_for_status(response, method)
                try:
                    return response.json()
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise GitHubError(
                        f"GitHub API returned invalid JSON for {response.request.url.path}"
                    ) from exc
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if not should_retry or attempt + 1 >= attempts:
                    break
                self._sleep(min(2**attempt, 4))
            except GitHubError as exc:
                last_error = exc
                if not (
                    should_retry
                    and isinstance(exc, GitHubRateLimitError)
                    and attempt + 1 < attempts
                ):
                    raise
                self._sleep(min(2**attempt, 4))

        safe_error = _redact(
            type(last_error).__name__ if last_error else "unknown error",
            [self.token or ""],
        )
        raise GitHubError(
            f"GitHub API {method} failed after {attempts} attempt(s): {safe_error}"
        ) from last_error

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request_json("GET", path, params=params)

    def post_json(self, path: str, body: Mapping[str, Any]) -> Any:
        return self.request_json("POST", path, json_body=body, retry=False)

    def resolve_pr(self, pr_url: str) -> PRInfo:
        """Resolve immutable base/head SHAs for a pull request."""

        owner, repo, number = parse_pr_url(pr_url)
        value = self.get_json(f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(value, Mapping):
            raise GitHubError("GitHub pull-request response is not an object")
        try:
            base = value["base"]
            head = value["head"]
            base_repo = base["repo"]
            base_sha = str(base["sha"])
            head_sha = str(head["sha"])
            clone_url = str(base_repo["clone_url"])
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_sha):
                raise ValueError("invalid base SHA")
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha):
                raise ValueError("invalid head SHA")
            if not clone_url.startswith("https://github.com/"):
                raise ValueError("unexpected clone host")
            return PRInfo(
                url=f"https://github.com/{owner}/{repo}/pull/{number}",
                owner=owner,
                repo=repo,
                number=number,
                clone_url=clone_url,
                title=str(value.get("title") or ""),
                body=str(value.get("body") or ""),
                base_ref=str(base["ref"]),
                base_sha=base_sha.lower(),
                head_ref=str(head["ref"]),
                head_sha=head_sha.lower(),
                resolved_at=utc_now(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(
                f"GitHub response for {owner}/{repo}#{number} lacks valid base/head metadata"
            ) from exc


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
        raise TypeError("to_dict() must return a mapping")
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"expected a review artifact, got {type(value).__name__}")


def _finding_body(finding: Mapping[str, Any]) -> str:
    title = str(finding.get("title") or "Code issue").strip()
    severity = str(finding.get("severity") or "medium").upper()
    body = str(finding.get("body") or "").strip()
    trigger = str(finding.get("trigger") or "").strip()
    impact = str(finding.get("impact") or "").strip()
    evidence_value = finding.get("evidence")
    evidence = evidence_value.strip() if isinstance(evidence_value, str) else ""
    fix = str(finding.get("suggested_fix") or "").strip()
    sections = [f"**[{severity}] {title}**"]
    if body and not trigger and not impact:
        sections.append(body)
    if trigger:
        sections.append(f"Trigger: {trigger}")
    if impact:
        sections.append(f"Impact: {impact}")
    if evidence and evidence not in body:
        sections.append(f"Evidence: {evidence}")
    if fix:
        sections.append(f"Suggested fix: {fix}")
    rendered = "\n\n".join(sections)
    if len(rendered.encode("utf-8")) > _MAX_COMMENT_BYTES:
        raise ValueError(f"finding comment is larger than {_MAX_COMMENT_BYTES} bytes")
    return rendered


def _review_identity(pr: PRInfo, artifact: Mapping[str, Any]) -> str:
    findings = artifact.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("review artifact findings must be an array")
    config = artifact.get("config") if isinstance(artifact.get("config"), Mapping) else {}
    identity = {
        "schema": "bugbunny-github-review-v1",
        "tool": str(artifact.get("tool") or "bugbunny"),
        "tool_version": str(artifact.get("tool_version") or ""),
        "model": str(config.get("model") or ""),
        "base_sha": pr.base_sha,
        "head_sha": pr.head_sha,
        "findings": [
            {
                "id": str(item.get("finding_id") or item.get("fingerprint") or ""),
                "path": str(item.get("path") or ""),
                "side": str(item.get("side") or "RIGHT").upper(),
                "line": int(item.get("line") or item.get("start_line") or 0),
                "end_line": int(
                    item.get("end_line") or item.get("line") or item.get("start_line") or 0
                ),
                "body": _finding_body(item),
            }
            for item in findings
            if isinstance(item, Mapping)
        ],
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]


def review_marker(pr: PRInfo, artifact: Any) -> str:
    """Return the deterministic idempotency marker for an artifact."""

    return f"<!-- bugbunny-review:v1:{_review_identity(pr, _as_mapping(artifact))} -->"


_PUBLISH_LOCKS: dict[str, threading.Lock] = {}
_PUBLISH_LOCKS_GUARD = threading.Lock()


def _lock_for(marker: str) -> threading.Lock:
    with _PUBLISH_LOCKS_GUARD:
        return _PUBLISH_LOCKS.setdefault(marker, threading.Lock())


def _default_publication_coordination_dir() -> Path:
    configured = os.environ.get("BUGBUNNY_PUBLISH_COORDINATION_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return (root / "bugbunny" / "publication-locks").resolve()


class GitHubReviewPublisher:
    """Publish a completed BugBunny artifact as one atomic GitHub review.

    A dedicated CodeReviewBench fixture is needed for published evaluations.
    Reusing a fixture owned by another tool would make the benchmark's
    repository-name parser misattribute BugBunny's comments and can also mix
    two bots' output. Existing fixtures are safe to reuse only for read-only/
    local runs; publish into ``__bugbunny-<model>__`` fixtures.

    Threads and processes that share ``coordination_dir`` are serialized around
    the remote GET-then-POST critical section. This is intentionally described
    as local-filesystem coordination: GitHub's review-create API has no
    conditional create/idempotency key, so simultaneous publishers on different
    hosts without a shared filesystem can still both observe no marker and post.
    """

    coordination_scope = "shared-local-filesystem"

    def __init__(
        self,
        client: GitHubClient,
        *,
        coordination_dir: Path | str | None = None,
    ) -> None:
        self.client = client
        self.coordination_dir = (
            Path(coordination_dir).expanduser().resolve()
            if coordination_dir is not None
            else _default_publication_coordination_dir()
        )

    def _coordination_lock_path(self, marker: str) -> Path:
        digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        return self.coordination_dir / f"{digest}.lock"

    def _existing_review(self, pr: PRInfo, marker: str) -> Mapping[str, Any] | None:
        page = 1
        while page <= 100:
            value = self.client.get_json(
                f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(value, list):
                raise GitHubError("GitHub reviews response is not an array")
            for review in value:
                if isinstance(review, Mapping) and marker in str(review.get("body") or ""):
                    return review
            if len(value) < 100:
                return None
            page += 1
        raise GitHubError("GitHub review pagination exceeded 100 pages")

    @staticmethod
    def _validate_target(pr: PRInfo, artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
        if artifact.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ValueError("only a native BugBunny review artifact can be published")
        if artifact.get("tool") != "bugbunny" or artifact.get("tool_version") != __version__:
            raise ValueError("review artifact does not match this BugBunny version")
        if artifact.get("implementation") != implementation_identity():
            raise ValueError("review artifact was produced by a different BugBunny implementation")
        if artifact.get("status") != "completed":
            raise ValueError("only a completed review artifact can be published")
        coverage = artifact.get("coverage")
        diff = artifact.get("diff")
        if not isinstance(coverage, Mapping) or coverage.get("complete") is not True:
            raise ValueError("only an artifact with complete diff coverage can be published")
        if not isinstance(diff, Mapping) or diff.get("chunk_plan_complete") is not True:
            raise ValueError("review artifact lacks a complete chunk-plan proof")
        artifact_pr = artifact.get("pr")
        if not isinstance(artifact_pr, Mapping):
            raise ValueError("review artifact lacks resolved pull-request identity")
        artifact_url = artifact_pr.get("url") or artifact_pr.get("pr_url")
        if not artifact_url or canonical_pr_url(str(artifact_url)) != pr.url:
            raise ValueError("review artifact targets a different pull request")
        artifact_head = artifact_pr.get("head_sha")
        if not artifact_head or str(artifact_head).lower() != pr.head_sha.lower():
            raise ValueError("review artifact was produced for a different head SHA")
        artifact_base = artifact_pr.get("base_sha")
        if not artifact_base or str(artifact_base).lower() != pr.base_sha.lower():
            raise ValueError("review artifact was produced for a different base SHA")

        raw_findings = artifact.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValueError("review artifact findings must be an array")
        comments: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, int, str]] = set()
        for index, raw in enumerate(raw_findings):
            if not isinstance(raw, Mapping):
                raise ValueError(f"finding {index} is not an object")
            path = str(raw.get("path") or "")
            candidate_path = PurePosixPath(path)
            if (
                not path
                or candidate_path.is_absolute()
                or ".." in candidate_path.parts
                or "\x00" in path
                or "\n" in path
                or "\r" in path
            ):
                raise ValueError(f"finding {index} has an unsafe path")
            line = int(raw.get("line") or raw.get("start_line") or 0)
            end_line = int(raw.get("end_line") or line)
            side = str(raw.get("side") or "RIGHT").upper()
            if line <= 0 or end_line < line:
                raise ValueError(f"finding {index} has an invalid line range")
            if side not in {"RIGHT", "LEFT"}:
                raise ValueError(f"finding {index} has an invalid diff side")
            if not artifact_location_is_commentable(
                diff,
                path=path,
                side=side,
                line=line,
                end_line=end_line,
            ):
                raise ValueError(f"finding {index} is outside the changed-line ledger")
            body = _finding_body(raw)
            finding_key = str(raw.get("finding_id") or raw.get("fingerprint") or "")
            if not finding_key:
                raise ValueError(f"finding {index} has no stable identity")
            identity = (finding_key, path, line, end_line, body)
            if identity in seen:
                raise ValueError(f"finding {index} duplicates another final finding")
            seen.add(identity)
            # BugBunny findings are validated at one changed anchor. Publishing
            # a model-provided range can make GitHub reject the entire atomic
            # review when its end happens to be context/out of range, so keep
            # the public comment on that proven anchor.
            comment: dict[str, Any] = {
                "path": path,
                "line": line,
                "side": side,
                "body": body,
            }
            comments.append(comment)
        return comments

    def publish(
        self,
        pr: PRInfo,
        artifact: Any,
        *,
        publish_clean: bool = False,
    ) -> PublishResult:
        """Publish once per shared local coordination filesystem.

        The marker check also recovers an ambiguous local POST, but it cannot be
        an atomic server-side uniqueness constraint across independent hosts.
        """

        if not self.client.can_write:
            raise GitHubPermissionError("a GitHub token is required to publish a review")
        value = _as_mapping(artifact)
        if pr.owner.casefold() == "code-review-benchmark" and not re.search(
            r"__bugbunny(?:-|__)", pr.repo, flags=re.IGNORECASE
        ):
            raise ValueError(
                "refusing to publish into a borrowed CodeReviewBench fixture; "
                "use a dedicated bugbunny-* fixture"
            )
        benchmark = value.get("benchmark")
        if isinstance(benchmark, Mapping) and benchmark.get("suite") == "CodeReviewBench":
            fixture_tool = str(benchmark.get("fixture_tool") or "")
            if fixture_tool and not fixture_tool.startswith("bugbunny"):
                raise ValueError(
                    "refusing to publish into a borrowed CodeReviewBench fixture; "
                    "use a dedicated bugbunny-* fixture"
                )
        comments = self._validate_target(pr, value)
        marker = review_marker(pr, value)
        if not comments and not publish_clean:
            return PublishResult("clean_not_published", marker, 0)

        # The in-process lock covers platforms whose flock semantics do not
        # contend between two descriptors in one process; the durable file lock
        # extends the same critical section across local processes.
        with _lock_for(marker), file_lock(self._coordination_lock_path(marker)):
            existing = self._existing_review(pr, marker)
            if existing is not None:
                return PublishResult(
                    "already_published",
                    marker,
                    len(comments),
                    int(existing["id"]) if existing.get("id") is not None else None,
                    str(existing.get("html_url") or "") or None,
                )

            body = (
                f"BugBunny found {len(comments)} actionable issue"
                f"{'s' if len(comments) != 1 else ''}.\n\n{marker}"
                if comments
                else f"BugBunny found no actionable issues.\n\n{marker}"
            )
            payload = {
                "commit_id": pr.head_sha,
                "event": "COMMENT",
                "body": body,
                "comments": comments,
            }
            try:
                response = self.client.post_json(
                    f"/repos/{pr.owner}/{pr.repo}/pulls/{pr.number}/reviews", payload
                )
            except GitHubError as exc:
                # POST is never blindly retried. A transport timeout may mean
                # GitHub committed the review before the connection failed, so
                # confirm by marker once before reporting an error.
                try:
                    existing = self._existing_review(pr, marker)
                except GitHubError:
                    existing = None
                if existing is not None:
                    return PublishResult(
                        "already_published",
                        marker,
                        len(comments),
                        int(existing["id"]) if existing.get("id") is not None else None,
                        str(existing.get("html_url") or "") or None,
                    )
                raise GitHubPublishError(
                    f"GitHub review publish failed for {pr.owner}/{pr.repo}#{pr.number}"
                ) from exc
            if not isinstance(response, Mapping):
                raise GitHubPublishError("GitHub review publish returned an invalid response")
            return PublishResult(
                "published",
                marker,
                len(comments),
                int(response["id"]) if response.get("id") is not None else None,
                str(response.get("html_url") or "") or None,
            )


class GitHubPRResolver:
    """Content-addressed resolver cache retained for CLI convenience."""

    def __init__(
        self,
        cache_dir: Path | str,
        timeout: float = 30.0,
        retries: int = 3,
        *,
        client: GitHubClient | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.client = client or GitHubClient(timeout=timeout, retries=retries)

    def resolve(self, pr_url: str, *, refresh: bool = False) -> tuple[PRInfo, bool]:
        owner, repo, number = parse_pr_url(pr_url)
        cache_path = self.cache_dir / "metadata" / f"{owner}__{repo}__PR{number}.json"
        if cache_path.exists() and not refresh:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                info = PRInfo.from_dict(cached)
                if info.url == canonical_pr_url(pr_url):
                    return info, True
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
        info = self.client.resolve_pr(pr_url)
        atomic_write_json(cache_path, info.to_dict())
        return info, False


def resolve_pr_url(pr_url: str, *, client: GitHubClient | None = None) -> PRInfo:
    """Resolve a PR with a short-lived client when one is not supplied."""

    if client is not None:
        return client.resolve_pr(pr_url)
    with GitHubClient() as created:
        return created.resolve_pr(pr_url)


__all__ = [
    "GitHubClient",
    "GitHubError",
    "GitHubNotFoundError",
    "GitHubPRResolver",
    "GitHubPermissionError",
    "GitHubPublishError",
    "GitHubRateLimitError",
    "GitHubReviewPublisher",
    "PublishResult",
    "canonical_pr_url",
    "parse_pr_url",
    "resolve_pr_url",
    "review_marker",
]
