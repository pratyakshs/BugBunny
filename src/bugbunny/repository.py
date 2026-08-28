"""Immutable, shared Git snapshots for pull-request review.

All remotes share one content-addressed Git object pool. Each review gets a
fresh detached worktree materialized from raw blobs, without running checkout
filters, hooks, package managers, build tools, or any code from the repository.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from bugbunny.models import PRInfo

DEFAULT_MAX_BLOB_BYTES = 2_000_000
DEFAULT_MAX_DIFF_BYTES = 32_000_000
DEFAULT_MAX_TREE_BYTES = 32_000_000
DEFAULT_MAX_TREE_FILES = 1_000_000
DEFAULT_MAX_WORKTREE_BYTES = 4_000_000_000
DEFAULT_MAX_WORKTREE_FILES = 1_000_000
MAX_STDERR_BYTES = 256_000
_FULL_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# These config overrides prevent repository-controlled hooks and global
# filesystem monitors from executing during worktree administration. Content is
# populated via cat-file rather than checkout, so smudge/clean filters never run.
_GIT_SAFE_OPTIONS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "http.version=HTTP/1.1",
)


class _CommandResult(NamedTuple):
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: _CommandResult):
        super().__init__(
            f"command failed ({result.returncode}): {' '.join(result.args)}\n"
            f"{result.stderr.strip()}"
        )
        self.result = result


class CommandOutputLimitError(RuntimeError):
    def __init__(self, command: list[str], stream: str, limit: int):
        super().__init__(f"command {stream} exceeded {limit} bytes: {' '.join(command)}")
        self.command = command
        self.stream = stream
        self.limit = limit


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_command(
    command: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
    max_stdout_records: int | None = None,
) -> _CommandResult:
    """Run a non-shell command with streaming byte, record, and time caps."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": max_stdout_bytes, "stderr": max_stderr_bytes}
    records = 0
    record_limit_reached = False
    deadline = time.monotonic() + timeout
    exceeded: tuple[str, int] | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise TimeoutError(f"command timed out after {timeout}s: {' '.join(command)}")
            for key, _ in selector.select(min(0.1, remaining)):
                name: str = key.data
                block = os.read(key.fd, 65_536)
                if not block:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if name == "stdout" and max_stdout_records is not None:
                    allowed_end = 0
                    cursor = 0
                    while records < max_stdout_records:
                        end = block.find(b"\n", cursor)
                        if end < 0:
                            allowed_end = len(block)
                            break
                        records += 1
                        allowed_end = end + 1
                        cursor = end + 1
                    if records >= max_stdout_records:
                        block = block[:allowed_end]
                        record_limit_reached = True
                limit = limits[name]
                if limit is not None and len(buffers[name]) + len(block) > limit:
                    exceeded = (name, limit)
                    buffers[name].extend(block[: max(0, limit - len(buffers[name]))])
                else:
                    buffers[name].extend(block)
                if exceeded or record_limit_reached:
                    process.kill()
                    process.wait()
                    break
            if exceeded or record_limit_reached:
                break
    finally:
        for key in list(selector.get_map().values()):
            key.fileobj.close()
        selector.close()
    stdout = bytes(buffers["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(buffers["stderr"]).decode("utf-8", errors="replace")
    if exceeded:
        raise CommandOutputLimitError(command, exceeded[0], exceeded[1])
    returncode = 0 if record_limit_reached else process.wait()
    result = _CommandResult(list(command), returncode, stdout, stderr)
    if check and returncode != 0:
        raise CommandError(result)
    return result


class RepositoryError(RuntimeError):
    """An immutable repository snapshot could not be proven."""


class RepositorySafetyError(RepositoryError):
    """A repository path or tree entry would escape the isolated worktree."""


class RepositoryLimitError(RepositoryError):
    """A bounded repository operation exceeded its explicit cap."""


class _MissingMergeBaseError(RepositoryError):
    """Exact commits are present, but their shallow histories do not meet."""


@dataclass(frozen=True)
class GrepHit:
    path: str
    line: int
    text: str

    def as_tuple(self) -> tuple[str, int, str]:
        return self.path, self.line, self.text


def _validate_object_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _FULL_OBJECT_ID.fullmatch(normalized):
        raise RepositoryError(f"{label} must be a full 40- or 64-character Git object ID")
    return normalized


def _safe_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise RepositorySafetyError(f"unsafe repository path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RepositorySafetyError(f"unsafe repository path: {value!r}")
    normalized = str(path)
    return normalized


def _contained(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


def _git_args(*args: str) -> list[str]:
    return ["git", *_GIT_SAFE_OPTIONS, *args]


@dataclass
class RepositorySnapshot:
    object_dir: Path
    worktree_path: Path
    base_sha: str
    head_sha: str
    merge_base_sha: str | None
    remote_url: str
    remote_name: str
    cache_hit: bool
    diff_sha256: str
    command_timeout: int = 60
    _lock_path: Path | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def git_dir(self) -> Path:
        """Compatibility alias for callers that inspect the shared object store."""

        return self.object_dir

    @property
    def review_base_sha(self) -> str:
        """The immutable merge base whose tree the pull request changes."""

        if self.merge_base_sha is None:
            raise RepositoryError("snapshot has no merge base for pull-request review")
        return self.merge_base_sha

    def __enter__(self) -> RepositorySnapshot:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RepositoryError("repository snapshot is closed")

    def _git(self, *args: str, check: bool = True, timeout: int | None = None) -> str:
        self._require_open()
        result = run_command(
            _git_args(f"--git-dir={self.object_dir}", *args),
            check=check,
            timeout=timeout or self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        )
        return result.stdout

    def diff(
        self,
        context_lines: int = 12,
        *,
        max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    ) -> str:
        self._require_open()
        if context_lines < 0:
            raise ValueError("context_lines cannot be negative")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        try:
            result = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--full-index",
                    "--find-renames",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    f"--unified={context_lines}",
                    self.review_base_sha,
                    self.head_sha,
                    "--",
                ),
                timeout=max(self.command_timeout, 180),
                max_stdout_bytes=max_bytes,
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(f"review-base/head diff exceeds {max_bytes} bytes") from exc
        # The immutable diff was hashed at acquisition. Rechecking it catches
        # object-store corruption or mutation between stages.
        if context_lines == 12 and sha256_text(result.stdout) != self.diff_sha256:
            raise RepositoryError(
                "immutable review-base/head diff changed after snapshot validation"
            )
        return result.stdout

    def read_blob(
        self,
        revision: str,
        path: str,
        *,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> str:
        self._require_open()
        path = _safe_relative_path(path)
        if revision not in {self.base_sha, self.review_base_sha, self.head_sha}:
            raise RepositorySafetyError(
                "reads are restricted to the snapshot's exact base/review-base/head commits"
            )
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        try:
            result = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "cat-file",
                    "blob",
                    f"{revision}:{path}",
                ),
                timeout=self.command_timeout,
                check=False,
                max_stdout_bytes=max_bytes,
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(
                f"refusing to read {path}: blob exceeds {max_bytes} bytes"
            ) from exc
        if result.returncode != 0:
            if "�" in path:
                # The diff/tree layers decode Git output with errors="replace",
                # so a non-UTF-8 filename reaches here with U+FFFD and can no
                # longer name the real object. Say so explicitly instead of a
                # bare miss.
                raise FileNotFoundError(
                    f"{path} cannot be read at {revision}: the path contains U+FFFD "
                    "from lossy UTF-8 decoding; non-UTF-8 Git filenames are "
                    "unsupported for content reads"
                )
            raise FileNotFoundError(f"{path} does not exist at {revision}: {result.stderr.strip()}")
        return result.stdout

    def read_file(
        self,
        revision: str,
        path: str,
        *,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> str:
        """Compatibility alias for immutable object reads."""

        return self.read_blob(revision, path, max_bytes=max_bytes)

    def read_text(self, path: str, *, max_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> str:
        """Read a bounded head-worktree file without following any symlink.

        Every existing path component is resolved and checked against the
        worktree root. The final entry must be a regular file. This rejects both
        direct symlinks and a parent-directory symlink escape.
        """

        self._require_open()
        path = _safe_relative_path(path)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        root = self.worktree_path.resolve(strict=True)
        lexical = root.joinpath(*PurePosixPath(path).parts)
        parent = lexical.parent.resolve(strict=True)
        if not _contained(root, parent):
            raise RepositorySafetyError(f"path escapes the worktree through a symlink: {path!r}")
        if lexical.is_symlink():
            raise RepositorySafetyError(f"refusing to follow repository symlink: {path!r}")
        resolved = lexical.resolve(strict=True)
        if not _contained(root, resolved):
            raise RepositorySafetyError(f"path escapes the worktree: {path!r}")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode):
            raise RepositorySafetyError(f"repository path is not a regular file: {path!r}")
        with resolved.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise RepositoryLimitError(f"refusing to read {path}: file exceeds {max_bytes} bytes")
        return payload.decode("utf-8", errors="replace")

    def list_files(
        self,
        revision: str | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_TREE_BYTES,
        max_files: int = DEFAULT_MAX_TREE_FILES,
    ) -> list[str]:
        self._require_open()
        revision = revision or self.head_sha
        if revision not in {self.base_sha, self.review_base_sha, self.head_sha}:
            raise RepositorySafetyError(
                "tree listing is restricted to exact base/review-base/head commits"
            )
        if max_bytes <= 0 or max_files <= 0:
            raise ValueError("tree limits must be positive")
        try:
            result = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "ls-tree",
                    "-rz",
                    "--name-only",
                    "--full-tree",
                    revision,
                ),
                timeout=max(self.command_timeout, 180),
                max_stdout_bytes=max_bytes,
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(
                f"refusing to list tree {revision}: output exceeds {max_bytes} bytes"
            ) from exc
        files = [item for item in result.stdout.split("\0") if item]
        if len(files) > max_files:
            raise RepositoryLimitError(
                f"refusing to list tree {revision}: more than {max_files} files"
            )
        return files

    def git_grep(
        self,
        pattern: str,
        *,
        revision: str | None = None,
        limit: int = 20,
        fixed: bool = True,
        word: bool = False,
        paths: Iterable[str] | None = None,
        literal_paths: bool = False,
        timeout: int = 15,
    ) -> tuple[GrepHit, ...]:
        """Search the entire immutable tree with real ``git grep`` semantics."""

        self._require_open()
        revision = revision or self.head_sha
        if revision not in {self.base_sha, self.review_base_sha, self.head_sha}:
            raise RepositorySafetyError("grep is restricted to exact base/review-base/head commits")
        if not pattern or "\x00" in pattern or "\n" in pattern or limit <= 0:
            return ()
        command = _git_args(
            f"--git-dir={self.object_dir}",
            "grep",
            "-n",
            "-z",
            "-I",
            "--full-name",
        )
        if fixed:
            command.append("-F")
        if word:
            command.append("-w")
        command.extend(("-e", pattern, revision, "--"))
        if paths is not None:
            safe_paths = sorted({_safe_relative_path(path) for path in paths})
            if not safe_paths:
                return ()
            command.extend(
                f":(top,literal){path}" if literal_paths else path for path in safe_paths
            )
        # A source line can be long. The byte cap is independent of the record
        # cap so either kind of unbounded output fails closed.
        # Keep the operation bounded, but allow a single long source line to
        # reach the caller.  Generated SVG/JSON files commonly contain lines
        # larger than the old 256 KiB floor; treating one such match as an
        # infrastructure failure makes model-directed exploration brittle.
        byte_limit = max(2_000_000, min(16_000_000, limit * 128_000))
        try:
            result = run_command(
                command,
                timeout=timeout,
                check=False,
                max_stdout_bytes=byte_limit,
                max_stderr_bytes=MAX_STDERR_BYTES,
                max_stdout_records=limit,
            )
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(f"git grep output exceeds {byte_limit} bytes") from exc
        if result.returncode not in (0, 1):
            raise RepositoryError(f"git grep failed ({result.returncode}): {result.stderr.strip()}")
        hits: list[GrepHit] = []
        prefix = f"{revision}:"
        # ``git grep -z -n`` emits ``{rev}:{path}\0{line}\0{text}\n`` with the
        # path field RAW (unquoted): a hostile filename may itself contain the
        # record-terminating LF, so the stream must not be pre-split on
        # newlines. Fields are tokenized on NUL instead; the matched text can
        # contain neither NUL (binary detection excludes such files) nor LF
        # (one source line per match), so the first LF after the second NUL
        # always ends the record. Lone \r, \f, \v, \x1c-\x1e, \x85, and
        # U+2028/29 remain ordinary bytes in both fields.
        stream = result.stdout
        position = 0
        while position < len(stream):
            first = stream.find("\0", position)
            second = stream.find("\0", first + 1) if first != -1 else -1
            newline = stream.find("\n", second + 1) if second != -1 else -1
            if newline == -1:
                raise RepositoryError("could not parse NUL-delimited git grep output")
            path_field = stream[position:first]
            line_field = stream[first + 1 : second]
            text = stream[second + 1 : newline]
            position = newline + 1
            if not line_field.isdigit():
                raise RepositoryError("could not parse NUL-delimited git grep output")
            path = _safe_relative_path(path_field.removeprefix(prefix))
            hits.append(GrepHit(path=path, line=int(line_field), text=text))
            if len(hits) >= limit:
                break
        return tuple(hits)

    def grep(
        self,
        revision: str,
        symbol: str,
        *,
        limit: int = 20,
        timeout: int = 15,
        paths: Iterable[str] | None = None,
    ) -> list[tuple[str, int, str]]:
        """Tuple-returning compatibility wrapper around whole-tree ``git_grep``."""

        return [
            hit.as_tuple()
            for hit in self.git_grep(
                symbol,
                revision=revision,
                limit=limit,
                fixed=True,
                word=True,
                paths=paths,
                timeout=timeout,
            )
        ]

    def assert_clean(self) -> None:
        self._require_open()
        head = run_command(
            _git_args("-C", str(self.worktree_path), "rev-parse", "HEAD^{commit}"),
            timeout=self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        ).stdout.strip()
        if head != self.head_sha:
            raise RepositoryError(f"worktree HEAD changed: expected {self.head_sha}, found {head}")
        status = run_command(
            _git_args(
                "-C",
                str(self.worktree_path),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            timeout=max(self.command_timeout, 180),
            max_stdout_bytes=2_000_000,
            max_stderr_bytes=MAX_STDERR_BYTES,
        ).stdout
        if status:
            raise RepositoryError("isolated worktree is not clean")

    def close(self) -> None:
        if self._closed:
            return
        root = self.worktree_path.parent.resolve()
        target = self.worktree_path.resolve(strict=False)
        if not _contained(root, target) or target == root:
            raise RepositorySafetyError("refusing to remove an uncontained worktree")
        lock_path = self._lock_path or (self.object_dir.parent / "object-pool.lock")
        with file_lock(lock_path):
            result = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "worktree",
                    "remove",
                    "--force",
                    str(self.worktree_path),
                ),
                check=False,
                timeout=max(self.command_timeout, 180),
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
            if result.returncode != 0 and self.worktree_path.exists():
                # The target is a UUID-named child of the dedicated cache dir.
                shutil.rmtree(self.worktree_path)
                run_command(
                    _git_args(f"--git-dir={self.object_dir}", "worktree", "prune"),
                    check=False,
                    timeout=self.command_timeout,
                    max_stderr_bytes=MAX_STDERR_BYTES,
                )
        self._closed = True


@dataclass(frozen=True)
class RepositoryPreparation:
    """Immutable repository identity prepared without creating a worktree."""

    base_sha: str
    head_sha: str
    merge_base_sha: str
    diff_sha256: str
    diff_bytes: int
    cache_hit: bool


class GitRepositoryCache:
    """One shared content-addressed object pool with per-run worktrees."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        command_timeout: int = 60,
        history_depth: int = 64,
        max_worktree_bytes: int = DEFAULT_MAX_WORKTREE_BYTES,
        max_worktree_files: int = DEFAULT_MAX_WORKTREE_FILES,
        shard_by_remote: bool = False,
        _direct_store: bool = False,
    ) -> None:
        if command_timeout <= 0 or history_depth <= 0:
            raise ValueError("command_timeout and history_depth must be positive")
        if max_worktree_bytes <= 0 or max_worktree_files <= 0:
            raise ValueError("worktree limits must be positive")
        self.cache_dir = cache_dir.expanduser().resolve()
        self.object_dir = self.cache_dir / "git" / "object-pool.git"
        self.worktrees_dir = self.cache_dir / "worktrees"
        self.lock_path = self.cache_dir / "locks" / "object-pool.lock"
        self.command_timeout = command_timeout
        self.history_depth = history_depth
        self.max_worktree_bytes = max_worktree_bytes
        self.max_worktree_files = max_worktree_files
        self.shard_by_remote = shard_by_remote
        self._direct_store = _direct_store

    def acquire(self, pr: PRInfo) -> RepositorySnapshot:
        result = self._acquire(
            remote_url=pr.clone_url,
            remote_label=pr.full_name,
            base_sha=pr.base_sha,
            head_sha=pr.head_sha,
            base_ref=pr.base_ref,
            head_ref=pr.head_ref,
            pull_number=pr.number,
        )
        if not isinstance(result, RepositorySnapshot):
            raise AssertionError("repository acquisition did not create a snapshot")
        return result

    def prepare(self, pr: PRInfo) -> RepositoryPreparation:
        """Fetch and validate immutable inputs without materializing a worktree."""

        result = self._acquire(
            remote_url=pr.clone_url,
            remote_label=pr.full_name,
            base_sha=pr.base_sha,
            head_sha=pr.head_sha,
            base_ref=pr.base_ref,
            head_ref=pr.head_ref,
            pull_number=pr.number,
            materialize=False,
        )
        if not isinstance(result, RepositoryPreparation):
            raise AssertionError("repository preparation unexpectedly created a snapshot")
        return result

    def from_local(self, path: Path, *, base_sha: str, head_sha: str) -> RepositorySnapshot:
        source = path.expanduser().resolve(strict=True)
        if not source.is_dir():
            raise RepositoryError(f"local repository is not a directory: {source}")
        result = self._acquire(
            remote_url=str(source),
            remote_label=source.name,
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=None,
            head_ref=None,
            pull_number=None,
        )
        if not isinstance(result, RepositorySnapshot):
            raise AssertionError("local repository acquisition did not create a snapshot")
        return result

    def _remote_store(self, remote_url: str) -> GitRepositoryCache:
        if not self.shard_by_remote or self._direct_store:
            return self
        remote_key = sha256_text(remote_url)[:20]
        return GitRepositoryCache(
            self.cache_dir / "repositories" / remote_key,
            command_timeout=self.command_timeout,
            history_depth=self.history_depth,
            max_worktree_bytes=self.max_worktree_bytes,
            max_worktree_files=self.max_worktree_files,
            shard_by_remote=False,
            _direct_store=True,
        )

    def _git(self, *args: str, check: bool = True, timeout: int | None = None) -> str:
        return run_command(
            _git_args(*args),
            check=check,
            timeout=timeout or self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        ).stdout

    def _ensure_pool(self) -> None:
        if self.object_dir.exists():
            if not (self.object_dir / "HEAD").is_file():
                raise RepositoryError(f"invalid Git object pool: {self.object_dir}")
            return
        self.object_dir.parent.mkdir(parents=True, exist_ok=True)
        self._git("init", "--bare", "--quiet", str(self.object_dir))

    def _ensure_remote(self, remote_name: str, remote_url: str) -> None:
        result = run_command(
            _git_args(
                f"--git-dir={self.object_dir}",
                "remote",
                "get-url",
                remote_name,
            ),
            check=False,
            timeout=self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        )
        if result.returncode == 0:
            if result.stdout.strip() != remote_url:
                raise RepositoryError(
                    f"cache remote collision for {remote_name}: URL does not match"
                )
            return
        self._git(
            f"--git-dir={self.object_dir}",
            "remote",
            "add",
            remote_name,
            remote_url,
        )

    def _object_is_exact_commit(self, sha: str) -> bool:
        result = run_command(
            _git_args(
                f"--git-dir={self.object_dir}",
                "rev-parse",
                "--verify",
                f"{sha}^{{commit}}",
            ),
            check=False,
            timeout=self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == sha

    def _fetch_exact(
        self,
        *,
        remote_name: str,
        remote_key: str,
        sha: str,
        label: str,
        fallback_refs: Iterable[str],
    ) -> tuple[str, bool, str]:
        target_ref = f"refs/bugbunny/{remote_key}/{label}/{sha}"
        if self._object_is_exact_commit(sha):
            self._git(f"--git-dir={self.object_dir}", "update-ref", target_ref, sha)
            return target_ref, True, sha

        temporary = f"refs/bugbunny/tmp/{remote_key}/{uuid.uuid4().hex}"
        attempts = [sha, *fallback_refs]
        errors: list[str] = []
        for source in attempts:
            result = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "fetch",
                    "--force",
                    "--no-tags",
                    f"--depth={self.history_depth}",
                    remote_name,
                    f"{source}:{temporary}",
                ),
                check=False,
                timeout=max(self.command_timeout, 300),
                max_stdout_bytes=1_000_000,
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
            if result.returncode != 0:
                errors.append(result.stderr.strip()[:500])
                continue
            if self._object_is_exact_commit(sha):
                self._git(f"--git-dir={self.object_dir}", "update-ref", target_ref, sha)
                self._git(
                    f"--git-dir={self.object_dir}",
                    "update-ref",
                    "-d",
                    temporary,
                    check=False,
                )
                return target_ref, False, source
            self._git(
                f"--git-dir={self.object_dir}",
                "update-ref",
                "-d",
                temporary,
                check=False,
            )
        details = "; ".join(error for error in errors if error)[:1_500]
        raise RepositoryError(
            f"could not fetch exact {label} commit {sha}; no fallback was accepted"
            + (f": {details}" if details else "")
        )

    def _recover_shallow_merge_base(
        self,
        *,
        remote_name: str,
        base_sha: str,
        head_sha: str,
        base_target: str,
        head_target: str,
        base_sources: Iterable[str],
        head_sources: Iterable[str],
    ) -> tuple[str, str | None, int]:
        """Complete shallow histories only when the exact tips do not meet.

        ``history_depth`` is an initial transfer optimization, not a semantic
        limit on which pull requests can be reviewed.  A deep divergence may
        put the real merge base just beyond both shallow boundaries.  Retry
        with the exact source refspecs that fetched the tips, falling back to
        the advertised PR/base refs when a server disallows fetching by SHA.
        """

        shallow = (
            self._git(
                f"--git-dir={self.object_dir}",
                "rev-parse",
                "--is-shallow-repository",
            )
            .strip()
            .lower()
            == "true"
        )
        if not shallow:
            raise _MissingMergeBaseError(
                "base and head have no valid merge base for pull-request review"
            )

        base_candidates = tuple(dict.fromkeys(base_sources))
        head_candidates = tuple(dict.fromkeys(head_sources))
        errors: list[str] = []
        attempted = False
        for base_source in base_candidates:
            for head_source in head_candidates:
                still_shallow = (
                    self._git(
                        f"--git-dir={self.object_dir}",
                        "rev-parse",
                        "--is-shallow-repository",
                    )
                    .strip()
                    .lower()
                    == "true"
                )
                depth_option = ["--unshallow"] if still_shallow else []
                result = run_command(
                    _git_args(
                        f"--git-dir={self.object_dir}",
                        "fetch",
                        "--force",
                        "--no-tags",
                        *depth_option,
                        remote_name,
                        f"{base_source}:{base_target}",
                        f"{head_source}:{head_target}",
                    ),
                    check=False,
                    timeout=max(self.command_timeout, 300),
                    max_stdout_bytes=1_000_000,
                    max_stderr_bytes=MAX_STDERR_BYTES,
                )
                attempted = True
                if result.returncode != 0:
                    errors.append(result.stderr.strip()[:500])
                    continue

                # A fallback branch may have moved since PR resolution. Keep
                # the cache refs pinned to the already-verified immutable tips.
                self._git(f"--git-dir={self.object_dir}", "update-ref", base_target, base_sha)
                self._git(f"--git-dir={self.object_dir}", "update-ref", head_target, head_sha)
                try:
                    return self._validate_diff(base_sha, head_sha)
                except _MissingMergeBaseError as exc:
                    errors.append(str(exc))

        details = "; ".join(error for error in errors if error)[:1_500]
        raise RepositoryError(
            "could not recover a merge base after completing shallow base/head histories"
            + (f": {details}" if attempted and details else "")
        )

    def _materialize_worktree(self, worktree: Path, head_sha: str) -> None:
        """Populate an index and files from raw blobs, bypassing all filters."""

        self._git("-C", str(worktree), "read-tree", head_sha)
        command = _git_args(
            f"--git-dir={self.object_dir}",
            "ls-tree",
            "-rz",
            "-r",
            "-l",
            "--full-tree",
            head_sha,
        )
        try:
            result = run_command(
                command,
                check=False,
                timeout=max(self.command_timeout, 300),
                max_stdout_bytes=DEFAULT_MAX_TREE_BYTES,
                max_stderr_bytes=MAX_STDERR_BYTES,
            )
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(
                "refusing to enumerate worktree: tree listing exceeds "
                f"{DEFAULT_MAX_TREE_BYTES} bytes"
            ) from exc
        if result.returncode != 0:
            raise RepositoryError("failed to enumerate worktree: " + result.stderr[:1_000])

        entries: list[tuple[str, str, int, str]] = []
        gitlinks: list[str] = []
        total_bytes = 0
        for record in result.stdout.split("\0"):
            if not record:
                continue
            try:
                metadata, path = record.split("\t", 1)
                mode, kind, oid, size_text = metadata.split(" ", 3)
                size_text = size_text.strip()
            except ValueError as exc:
                raise RepositoryError("could not parse ls-tree output") from exc
            path = _safe_relative_path(path)
            if kind == "commit":
                # Keep gitlinks uninitialized while creating their empty mount
                # directories. An absent mount is reported as a deleted
                # gitlink, whereas an empty directory matches a normal
                # worktree before `git submodule update`.
                gitlinks.append(path)
                if len(entries) + len(gitlinks) > self.max_worktree_files:
                    raise RepositoryLimitError(
                        f"worktree has more than {self.max_worktree_files} files"
                    )
                continue
            if kind != "blob" or not size_text.isdigit():
                raise RepositoryError(f"unsupported Git tree entry at {path}")
            size = int(size_text)
            total_bytes += size
            if total_bytes > self.max_worktree_bytes:
                raise RepositoryLimitError(f"worktree exceeds {self.max_worktree_bytes} bytes")
            entries.append((mode, oid, size, path))
            if len(entries) + len(gitlinks) > self.max_worktree_files:
                raise RepositoryLimitError(
                    f"worktree has more than {self.max_worktree_files} files"
                )

        process = subprocess.Popen(
            _git_args(f"--git-dir={self.object_dir}", "cat-file", "--batch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise RepositoryError("could not start bounded Git blob reader")
        root = worktree.resolve(strict=True)
        try:
            for path in gitlinks:
                target = root.joinpath(*PurePosixPath(path).parts)
                if not _contained(root, target):
                    raise RepositorySafetyError(f"tree path escapes worktree: {path!r}")
                target.mkdir(parents=True, exist_ok=True)
            for mode, oid, expected_size, path in entries:
                process.stdin.write(oid.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline()
                fields = header.rstrip(b"\n").split(b" ")
                if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
                    raise RepositoryError(f"invalid cat-file response for {path}")
                size = int(fields[2])
                if size != expected_size:
                    raise RepositoryError(f"Git blob size changed while reading {path}")
                target = root.joinpath(*PurePosixPath(path).parts)
                if not _contained(root, target):
                    raise RepositorySafetyError(f"tree path escapes worktree: {path!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                remaining = size
                payload = bytearray() if mode == "120000" else None
                handle = None if payload is not None else target.open("wb")
                try:
                    while remaining:
                        block = process.stdout.read(min(1_048_576, remaining))
                        if not block:
                            raise RepositoryError(f"truncated Git blob for {path}")
                        if payload is not None:
                            payload.extend(block)
                        else:
                            assert handle is not None
                            handle.write(block)
                        remaining -= len(block)
                finally:
                    if handle is not None:
                        handle.close()
                if process.stdout.read(1) != b"\n":
                    raise RepositoryError(f"invalid cat-file delimiter for {path}")
                if payload is not None:
                    link_target = os.fsdecode(bytes(payload))
                    if not link_target or "\x00" in link_target:
                        raise RepositorySafetyError(f"unsafe symlink target in {path!r}")
                    candidate = (target.parent / link_target).resolve(strict=False)
                    if Path(link_target).is_absolute() or not _contained(root, candidate):
                        raise RepositorySafetyError(
                            f"repository symlink escapes worktree: {path!r} -> {link_target!r}"
                        )
                    target.symlink_to(link_target)
                else:
                    target.chmod(0o755 if mode == "100755" else 0o644)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            if process.returncode != 0:
                raise RepositoryError(f"Git blob reader failed: {stderr[:1_000]}")

    def _validate_diff(self, base_sha: str, head_sha: str) -> tuple[str, str | None, int]:
        for label, sha in (("base", base_sha), ("head", head_sha)):
            actual = (
                self._git(
                    f"--git-dir={self.object_dir}",
                    "rev-parse",
                    "--verify",
                    f"{sha}^{{commit}}",
                )
                .strip()
                .lower()
            )
            if actual != sha:
                raise RepositoryError(
                    f"{label} commit verification failed: expected {sha}, got {actual}"
                )
        merge_base_result = run_command(
            _git_args(
                f"--git-dir={self.object_dir}",
                "merge-base",
                base_sha,
                head_sha,
            ),
            check=False,
            timeout=self.command_timeout,
            max_stderr_bytes=MAX_STDERR_BYTES,
        )
        merge_base = merge_base_result.stdout.strip().lower()
        if merge_base_result.returncode == 1 and not merge_base:
            raise _MissingMergeBaseError(
                "base and head have no valid merge base for pull-request review"
            )
        if merge_base_result.returncode != 0 or not _FULL_OBJECT_ID.fullmatch(merge_base):
            raise RepositoryError(
                "Git could not compute a valid merge base for pull-request review: "
                + merge_base_result.stderr.strip()
            )
        actual_merge_base = (
            self._git(
                f"--git-dir={self.object_dir}",
                "rev-parse",
                "--verify",
                f"{merge_base}^{{commit}}",
            )
            .strip()
            .lower()
        )
        if actual_merge_base != merge_base:
            raise RepositoryError(
                "merge-base commit verification failed: "
                f"expected {merge_base}, got {actual_merge_base}"
            )
        probe = run_command(
            _git_args(
                f"--git-dir={self.object_dir}",
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                merge_base,
                head_sha,
                "--",
            ),
            check=False,
            timeout=max(self.command_timeout, 180),
            max_stderr_bytes=MAX_STDERR_BYTES,
        )
        if probe.returncode not in (0, 1):
            raise RepositoryError(
                f"Git could not validate the base/head diff: {probe.stderr.strip()}"
            )
        try:
            diff = run_command(
                _git_args(
                    f"--git-dir={self.object_dir}",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--full-index",
                    "--find-renames",
                    "--src-prefix=a/",
                    "--dst-prefix=b/",
                    "--unified=12",
                    merge_base,
                    head_sha,
                    "--",
                ),
                timeout=max(self.command_timeout, 180),
                max_stdout_bytes=DEFAULT_MAX_DIFF_BYTES,
                max_stderr_bytes=MAX_STDERR_BYTES,
            ).stdout
        except CommandOutputLimitError as exc:
            raise RepositoryLimitError(
                f"review-base/head diff exceeds {DEFAULT_MAX_DIFF_BYTES} bytes"
            ) from exc
        return sha256_text(diff), merge_base, len(diff.encode("utf-8"))

    def _acquire(
        self,
        *,
        remote_url: str,
        remote_label: str,
        base_sha: str,
        head_sha: str,
        base_ref: str | None,
        head_ref: str | None,
        pull_number: int | None,
        materialize: bool = True,
    ) -> RepositorySnapshot | RepositoryPreparation:
        store = self._remote_store(remote_url)
        if store is not self:
            return store._acquire(
                remote_url=remote_url,
                remote_label=remote_label,
                base_sha=base_sha,
                head_sha=head_sha,
                base_ref=base_ref,
                head_ref=head_ref,
                pull_number=pull_number,
                materialize=materialize,
            )
        base_sha = _validate_object_id(base_sha, "base_sha")
        head_sha = _validate_object_id(head_sha, "head_sha")
        if not remote_url.strip():
            raise RepositoryError("clone URL must not be empty")
        remote_key = sha256_text(remote_url)[:20]
        remote_name = f"r_{remote_key}"
        pair_key = sha256_text(f"{remote_url}\0{base_sha}\0{head_sha}")[:20]
        worktree = self.worktrees_dir / f"{pair_key}-{uuid.uuid4().hex}"
        if not _contained(self.worktrees_dir.resolve(strict=False), worktree.resolve(strict=False)):
            raise RepositorySafetyError("computed worktree path escaped its cache root")

        with file_lock(self.lock_path):
            self._ensure_pool()
            self._ensure_remote(remote_name, remote_url)
            base_fallbacks = [f"refs/heads/{base_ref}"] if base_ref else []
            head_fallbacks: list[str] = []
            if pull_number and pull_number > 0:
                head_fallbacks.append(f"refs/pull/{pull_number}/head")
            if head_ref:
                head_fallbacks.append(f"refs/heads/{head_ref}")
            base_target, base_hit, base_source = self._fetch_exact(
                remote_name=remote_name,
                remote_key=remote_key,
                sha=base_sha,
                label="base",
                fallback_refs=base_fallbacks,
            )
            head_target, head_hit, head_source = self._fetch_exact(
                remote_name=remote_name,
                remote_key=remote_key,
                sha=head_sha,
                label="head",
                fallback_refs=head_fallbacks,
            )
            try:
                diff_hash, merge_base, diff_bytes = self._validate_diff(base_sha, head_sha)
            except _MissingMergeBaseError:
                diff_hash, merge_base, diff_bytes = self._recover_shallow_merge_base(
                    remote_name=remote_name,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    base_target=base_target,
                    head_target=head_target,
                    base_sources=(base_source, base_sha, *base_fallbacks),
                    head_sources=(head_source, head_sha, *head_fallbacks),
                )
            if not materialize:
                if merge_base is None:
                    raise RepositoryError("prepared pull request has no merge base")
                return RepositoryPreparation(
                    base_sha=base_sha,
                    head_sha=head_sha,
                    merge_base_sha=merge_base,
                    diff_sha256=diff_hash,
                    diff_bytes=diff_bytes,
                    cache_hit=base_hit and head_hit,
                )
            self.worktrees_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._git(
                    f"--git-dir={self.object_dir}",
                    "worktree",
                    "add",
                    "--detach",
                    "--no-checkout",
                    str(worktree),
                    head_sha,
                    timeout=max(self.command_timeout, 300),
                )
                self._materialize_worktree(worktree, head_sha)
            except Exception:
                if worktree.exists():
                    shutil.rmtree(worktree)
                self._git(
                    f"--git-dir={self.object_dir}",
                    "worktree",
                    "prune",
                    check=False,
                )
                raise

        snapshot = RepositorySnapshot(
            object_dir=self.object_dir,
            worktree_path=worktree,
            base_sha=base_sha,
            head_sha=head_sha,
            merge_base_sha=merge_base,
            remote_url=remote_url,
            remote_name=remote_name,
            cache_hit=base_hit and head_hit,
            diff_sha256=diff_hash,
            command_timeout=self.command_timeout,
            _lock_path=self.lock_path,
        )
        try:
            snapshot.assert_clean()
        except Exception:
            snapshot.close()
            raise
        return snapshot


__all__ = [
    "GitRepositoryCache",
    "GrepHit",
    "RepositoryError",
    "RepositoryLimitError",
    "RepositoryPreparation",
    "RepositorySafetyError",
    "RepositorySnapshot",
]
