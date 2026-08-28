from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_lines(text: str) -> list[str]:
    """Split source text exactly as Git numbers lines: only ``\\n`` terminates.

    ``str.splitlines()`` also breaks on ``\\r``, ``\\f``, ``\\v``,
    ``\\x1c``-``\\x1e``, ``\\x85``, and U+2028/U+2029, which desynchronizes
    Python line numbers from ``git diff``/``git grep`` numbering for files that
    contain those bytes. Here they are ordinary source characters. A trailing
    newline does not create a final empty line, matching Git's line count.
    """

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


async def acquire_semaphore_bounded(semaphore: Any, timeout_seconds: float) -> None:
    """Acquire an asyncio semaphore with a timeout without leaking a permit.

    ``asyncio.wait_for(semaphore.acquire(), t)`` on Python 3.11 (this
    package's floor) can cancel the acquire after the permit was already
    granted; the grant is then lost and the semaphore's effective capacity
    shrinks permanently. Waiting on the acquire as a separate task and
    releasing a late grant is correct on every supported interpreter.
    """

    import asyncio

    acquire_task = asyncio.ensure_future(semaphore.acquire())
    try:
        done, _pending = await asyncio.wait({acquire_task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        await _abandon_semaphore_acquire(semaphore, acquire_task)
        raise
    if acquire_task in done:
        acquire_task.result()
        return
    await _abandon_semaphore_acquire(semaphore, acquire_task)
    raise TimeoutError(f"semaphore acquisition exceeded {timeout_seconds:g}s")


async def _abandon_semaphore_acquire(semaphore: Any, acquire_task: Any) -> None:
    """Cancel a pending acquire, returning a permit granted mid-cancellation."""

    import asyncio

    acquire_task.cancel()
    try:
        await acquire_task
    except asyncio.CancelledError:
        return
    except Exception:
        return
    semaphore.release()


def is_finite_number(value: Any) -> bool:
    """True for a real, finite int/float; bools and huge ints are rejected.

    ``float()`` and ``math.isfinite()`` raise OverflowError for integers
    beyond float range, so wire and artifact validation must route such
    values through this check instead of calling either directly; a
    model-supplied 400-digit integer is an ordinary invalid value, not a
    crash.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def slugify(value: str, *, limit: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "value")[:limit].rstrip("-")


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory flock for a cross-process critical section."""

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, value: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # The rename itself is durable only once the containing directory is
        # synced; without this, a power loss can revert or drop a checkpoint,
        # manifest, or judge-invalidation commit that was already acknowledged.
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems refuse directory fsync; the write remains atomic
        # for concurrent readers even where crash durability is unavailable.
        pass
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
