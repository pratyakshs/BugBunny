"""Lossless, deterministic parsing and chunking for Git unified diffs.

The parser deliberately keeps Git's raw patch representation separate from the
line-number annotated representation sent to a model. Chunking repeats file
and hunk headers, but assigns every payload line to exactly one segment. The
coverage accounting therefore detects accidental omission or duplication.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

LineKind = Literal["add", "delete", "context", "meta"]
ExclusionKind = Literal[
    "binary",
    "generated",
    "vendor",
    "lockfile",
    "combined_diff",
    "metadata_only",
]


class DiffParseError(ValueError):
    """The patch is malformed or uses an unsupported ambiguous representation."""


class DiffChunkingError(ValueError):
    """A patch cannot be represented losslessly inside the requested cap."""


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$")
_LOCK_NAMES = {
    "bun.lock",
    "bun.lockb",
    "cargo.lock",
    "composer.lock",
    "deno.lock",
    "flake.lock",
    "gemfile.lock",
    "go.sum",
    "mix.lock",
    "package-lock.json",
    "packages.lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "pubspec.lock",
    "uv.lock",
    "yarn.lock",
}
_VENDOR_PARTS = {
    ".venv",
    "bower_components",
    "deps",
    "external",
    "node_modules",
    "third_party",
    "vendor",
    "vendors",
}
_GENERATED_PARTS = {"coverage", "dist", "generated", "genfiles"}
_GENERATED_NAME = re.compile(
    r"(?:\.generated\.|\.designer\.|\.min\.(?:js|css)$|\.g\.dart$|"
    r"\.pb\.(?:cc|h|go|rs)$|_pb2(?:_grpc)?\.py$|(?:^|_)generated\.)",
    re.IGNORECASE,
)


def _stable_id(*parts: object, size: int = 12) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:size]


def _split_physical_lines(raw: str) -> list[tuple[str, str]]:
    """Return ``(text, terminator)`` records without normalizing line endings."""

    records: list[tuple[str, str]] = []
    start = 0
    for index, character in enumerate(raw):
        if character != "\n":
            continue
        physical = raw[start:index]
        if physical.endswith("\r"):
            records.append((physical[:-1], "\r\n"))
        else:
            records.append((physical, "\n"))
        start = index + 1
    if start < len(raw):
        # Lone CR, VT, FF, NEL, and Unicode separators are source bytes, not
        # Git patch record terminators. Only LF (or CRLF) separates records.
        records.append((raw[start:], ""))
    return records


def _decode_git_quoted(value: str) -> str:
    """Decode the C-style path quoting emitted by Git's ``core.quotePath``.

    The token is taken verbatim: Git header filenames may legitimately begin
    or end with blanks (``rename from x ``), so trimming belongs to callers
    whose surrounding syntax requires it, never to the decoder.
    """

    if not (len(value) >= 2 and value[0] == '"' and value[-1] == '"'):
        return value
    source = value[1:-1]
    output = bytearray()
    index = 0
    escapes = {
        "a": 7,
        "b": 8,
        "t": 9,
        "n": 10,
        "v": 11,
        "f": 12,
        "r": 13,
        '"': 34,
        "\\": 92,
    }
    while index < len(source):
        char = source[index]
        if char != "\\":
            output.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(source):
            output.append(92)
            break
        escaped = source[index]
        if escaped in escapes:
            output.append(escapes[escaped])
            index += 1
            continue
        if escaped in "01234567":
            end = index
            while end < min(len(source), index + 3) and source[end] in "01234567":
                end += 1
            code = int(source[index:end], 8)
            if code > 0xFF:
                # Git never emits an octal escape above \377; keep a hostile
                # over-range sequence as literal text instead of crashing.
                output.append(92)
                output.extend(source[index:end].encode("utf-8"))
            else:
                output.append(code)
            index = end
            continue
        output.extend(escaped.encode("utf-8"))
        index += 1
    return output.decode("utf-8", errors="replace")


def _scan_git_tokens(value: str) -> list[str]:
    """Tokenize the quoted path pair following ``diff --git``."""

    tokens: list[str] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value):
            break
        if value[index] != '"':
            end = index
            while end < len(value) and not value[end].isspace():
                end += 1
            tokens.append(value[index:end])
            index = end
            continue
        end = index + 1
        escaped = False
        while end < len(value):
            char = value[end]
            if char == '"' and not escaped:
                end += 1
                break
            escaped = char == "\\" and not escaped
            end += 1
        if end > len(value) or value[end - 1 : end] != '"':
            raise DiffParseError("unterminated quoted path in diff --git header")
        tokens.append(value[index:end])
        index = end
    return tokens


def _git_header_paths(value: str) -> tuple[str | None, str | None]:
    """Parse the path pair following ``diff --git``.

    Git does not C-quote ordinary spaces, so a whitespace tokenizer cannot
    parse headers such as ``a/src/space name.py b/src/space name.py``.  The
    repository boundary fixes the conventional ``a/`` and ``b/`` prefixes;
    use those prefixes as the delimiter for unquoted headers.  When a path
    itself contains `` b/``, prefer the unique split whose two stripped paths
    agree (the ordinary modified/add/delete case).  Rename/copy metadata or
    the subsequent ``---``/``+++`` headers make an otherwise ambiguous
    provisional split exact before any hunk is created.
    """

    tokens = _scan_git_tokens(value)
    candidates: list[tuple[str, str]] = []
    for marker in (" b/", ' "b/'):
        start = 0
        while True:
            split_at = value.find(marker, start)
            if split_at < 0:
                break
            old_token = value[:split_at]
            new_token = value[split_at + 1 :]
            old_path = _strip_side_prefix(old_token)
            new_path = _strip_side_prefix(new_token)
            if old_path != old_token and new_path != new_token:
                candidates.append((old_token, new_token))
            start = split_at + 1

    if not candidates and len(tokens) == 2:
        return _strip_side_prefix(tokens[0]), _strip_side_prefix(tokens[1])
    if not candidates:
        raise DiffParseError(f"invalid diff --git path pair: {value!r}")
    matching = [
        pair for pair in candidates if _strip_side_prefix(pair[0]) == _strip_side_prefix(pair[1])
    ]
    old_token, new_token = matching[0] if len(matching) == 1 else candidates[-1]
    return _strip_side_prefix(old_token), _strip_side_prefix(new_token)


def _strip_side_prefix(path: str | None) -> str | None:
    if path is None:
        return None
    path = _decode_git_quoted(path)
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _header_path(value: str) -> str | None:
    if value.startswith('"'):
        tokens = _scan_git_tokens(value)
        token = tokens[0] if tokens else value
    else:
        token = value.split("\t", 1)[0]
    return _strip_side_prefix(token)


@dataclass(frozen=True)
class DiffLine:
    kind: LineKind
    content: str
    old_line: int | None
    new_line: int | None
    raw: str
    ending: str
    source_id: str

    @property
    def added_line(self) -> int | None:
        """The commentable added-side line, if this is an addition."""

        return self.new_line if self.kind == "add" else None

    def render_raw(self) -> str:
        return self.raw + self.ending

    def render_annotated(self) -> str:
        if self.kind == "add":
            coordinate = f"R{self.new_line}"
        elif self.kind == "delete":
            coordinate = f"L{self.old_line}"
        elif self.kind == "context":
            coordinate = f"R{self.new_line}/L{self.old_line}"
        else:
            coordinate = "META"
        return f"{coordinate:>15} | {self.raw}{self.ending}"


@dataclass
class Hunk:
    path: str
    old_path: str | None
    new_path: str | None
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    raw_header: str
    header_ending: str
    hunk_id: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        return {
            line.new_line for line in self.lines if line.kind == "add" and line.new_line is not None
        }

    @property
    def deleted_lines(self) -> set[int]:
        return {
            line.old_line
            for line in self.lines
            if line.kind == "delete" and line.old_line is not None
        }

    def render_raw(self) -> str:
        return (
            self.raw_header + self.header_ending + "".join(line.render_raw() for line in self.lines)
        )


@dataclass(frozen=True)
class FileExclusion:
    path: str
    kind: ExclusionKind
    reason: str
    file_index: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "kind": self.kind,
            "reason": self.reason,
            "file_index": self.file_index,
        }


@dataclass
class FileDiff:
    index: int
    old_path: str | None
    new_path: str | None
    status: str = "modified"
    is_binary: bool = False
    is_combined: bool = False
    header_lines: list[str] = field(default_factory=list)
    trailing_lines: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    raw_text: str = ""
    exclusion: FileExclusion | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or f"(unknown-file-{self.index})"

    @property
    def added_lines(self) -> set[int]:
        return {number for hunk in self.hunks for number in hunk.added_lines}

    @property
    def deleted_lines(self) -> set[int]:
        return {number for hunk in self.hunks for number in hunk.deleted_lines}

    @property
    def eligible(self) -> bool:
        return self.exclusion is None

    def render_raw(self) -> str:
        return self.raw_text


@dataclass(frozen=True)
class DiffSegment:
    segment_id: str
    hunk_id: str
    path: str
    segment_index: int
    raw_header: str
    header_ending: str
    lines: tuple[DiffLine, ...]

    @property
    def source_line_ids(self) -> tuple[str, ...]:
        return tuple(line.source_id for line in self.lines)

    def render_raw(self) -> str:
        return (
            self.raw_header + self.header_ending + "".join(line.render_raw() for line in self.lines)
        )

    def render_annotated(self) -> str:
        header = self.raw_header + self.header_ending
        return header + "".join(line.render_annotated() for line in self.lines)


@dataclass(frozen=True)
class DiffChunk:
    chunk_id: str
    path: str
    file_index: int
    chunk_index: int
    file_header: str
    segments: tuple[DiffSegment, ...]
    trailer: str
    max_chars: int

    @property
    def hunk_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(segment.hunk_id for segment in self.segments))

    @property
    def segment_ids(self) -> tuple[str, ...]:
        return tuple(segment.segment_id for segment in self.segments)

    @property
    def source_line_ids(self) -> tuple[str, ...]:
        return tuple(
            source_id for segment in self.segments for source_id in segment.source_line_ids
        )

    @property
    def added_lines(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    line.new_line
                    for segment in self.segments
                    for line in segment.lines
                    if line.kind == "add" and line.new_line is not None
                }
            )
        )

    @property
    def deleted_lines(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    line.old_line
                    for segment in self.segments
                    for line in segment.lines
                    if line.kind == "delete" and line.old_line is not None
                }
            )
        )

    @property
    def patch(self) -> str:
        return (
            self.file_header
            + "".join(segment.render_raw() for segment in self.segments)
            + self.trailer
        )

    @property
    def annotated_patch(self) -> str:
        return (
            self.file_header
            + "".join(segment.render_annotated() for segment in self.segments)
            + self.trailer
        )

    @property
    def text(self) -> str:
        """Model-facing alias: the addition-side annotated representation."""

        return self.annotated_patch

    @property
    def char_count(self) -> int:
        return len(self.annotated_patch)

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "path": self.path,
            "hunk_ids": list(self.hunk_ids),
            "segment_ids": list(self.segment_ids),
            "added_lines": list(self.added_lines),
            "deleted_lines": list(self.deleted_lines),
            "char_count": self.char_count,
            "max_chars": self.max_chars,
        }


@dataclass(frozen=True)
class ChunkPlan:
    chunks: tuple[DiffChunk, ...]
    exclusions: tuple[FileExclusion, ...]
    expected_source_line_ids: tuple[str, ...]
    max_chars: int
    total_files: int
    eligible_files: int

    @property
    def complete(self) -> bool:
        actual = [source_id for chunk in self.chunks for source_id in chunk.source_line_ids]
        return len(actual) == len(set(actual)) and set(actual) == set(self.expected_source_line_ids)

    def require_complete(self) -> None:
        if not self.complete:
            raise DiffChunkingError("chunk plan is not a lossless partition of eligible diff lines")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_chars": self.max_chars,
            "total_files": self.total_files,
            "eligible_files": self.eligible_files,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "exclusions": [exclusion.to_dict() for exclusion in self.exclusions],
            "complete": self.complete,
        }


@dataclass
class ParsedDiff:
    raw: str
    preamble: str
    files: list[FileDiff]

    @property
    def hunks(self) -> list[Hunk]:
        return [hunk for file_diff in self.files for hunk in file_diff.hunks]

    @property
    def added_lines(self) -> int:
        return sum(len(hunk.added_lines) for hunk in self.hunks)

    @property
    def deleted_lines(self) -> int:
        return sum(len(hunk.deleted_lines) for hunk in self.hunks)

    @property
    def exclusions(self) -> list[FileExclusion]:
        return [file_diff.exclusion for file_diff in self.files if file_diff.exclusion is not None]

    def changed_line_map(self) -> dict[str, set[int]]:
        """Map each right-side path to all commentable added line numbers."""

        result: dict[str, set[int]] = {}
        for file_diff in self.files:
            if file_diff.new_path is not None:
                result.setdefault(file_diff.new_path, set()).update(file_diff.added_lines)
        return result

    def deleted_line_map(self) -> dict[str, set[int]]:
        result: dict[str, set[int]] = {}
        for file_diff in self.files:
            if file_diff.old_path is not None:
                result.setdefault(file_diff.old_path, set()).update(file_diff.deleted_lines)
        return result

    def chunk(self, max_chars: int, *, include_excluded: bool = False) -> ChunkPlan:
        return chunk_diff(self, max_chars=max_chars, include_excluded=include_excluded)


def _classify_exclusion(file_diff: FileDiff) -> FileExclusion | None:
    path = file_diff.path
    pure = PurePosixPath(path)
    lowered_parts = {part.lower() for part in pure.parts}
    lowered_name = pure.name.lower()
    if file_diff.is_binary:
        return FileExclusion(
            path,
            "binary",
            "Git reports a binary patch; no textual lines are reviewable",
            file_diff.index,
        )
    if file_diff.is_combined:
        return FileExclusion(
            path,
            "combined_diff",
            "combined merge diffs are ambiguous for pull-request line comments",
            file_diff.index,
        )
    if lowered_name in _LOCK_NAMES or lowered_name.endswith((".lock", ".lock.json")):
        return FileExclusion(
            path,
            "lockfile",
            "dependency lockfile is machine-maintained and low-signal for semantic review",
            file_diff.index,
        )
    vendor = sorted(lowered_parts & _VENDOR_PARTS)
    if vendor:
        return FileExclusion(
            path,
            "vendor",
            f"vendored dependency path component: {vendor[0]}",
            file_diff.index,
        )
    generated_part = sorted(lowered_parts & _GENERATED_PARTS)
    # Generated-file exclusions must be derived from repository structure, not
    # newly added source text. A pull request controls every added comment and
    # could otherwise opt an arbitrary handwritten file out of review merely
    # by inserting an ``@generated`` marker near its beginning.
    if generated_part or _GENERATED_NAME.search(lowered_name):
        detail = (
            f"generated output path component: {generated_part[0]}"
            if generated_part
            else "generated-code filename"
        )
        return FileExclusion(path, "generated", detail, file_diff.index)
    if not file_diff.hunks:
        return FileExclusion(
            path,
            "metadata_only",
            "change has no textual unified-diff hunks",
            file_diff.index,
        )
    return None


def parse_unified_diff(raw: str, *, strict: bool = True) -> ParsedDiff:
    """Parse an ordinary Git unified diff while preserving its exact raw text.

    In strict mode, malformed range counts fail closed. Every file is retained,
    including binary and metadata-only changes, and receives an explicit
    exclusion reason when it cannot or should not enter model review.
    """

    records = _split_physical_lines(raw)
    files: list[FileDiff] = []
    preamble: list[str] = []
    current_file: FileDiff | None = None
    current_hunk: Hunk | None = None
    old_cursor = new_cursor = 0
    old_seen = new_seen = 0
    file_raw: list[str] = []

    def finish_hunk() -> None:
        nonlocal current_hunk, old_seen, new_seen
        if current_hunk is None:
            return
        if strict and (old_seen != current_hunk.old_count or new_seen != current_hunk.new_count):
            raise DiffParseError(
                f"hunk {current_hunk.hunk_id} range mismatch: expected "
                f"-{current_hunk.old_count}/+{current_hunk.new_count}, "
                f"saw -{old_seen}/+{new_seen}"
            )
        current_hunk = None
        old_seen = new_seen = 0

    def finish_file() -> None:
        nonlocal current_file, file_raw
        finish_hunk()
        if current_file is not None:
            current_file.raw_text = "".join(file_raw)
            current_file.exclusion = _classify_exclusion(current_file)
        file_raw = []

    for text, ending in records:
        physical = text + ending
        is_file_start = text.startswith(("diff --git ", "diff --cc ", "diff --combined "))
        if is_file_start:
            finish_file()
            index = len(files)
            is_combined = not text.startswith("diff --git ")
            old_path: str | None = None
            new_path: str | None = None
            if not is_combined:
                old_path, new_path = _git_header_paths(text[len("diff --git ") :])
            else:
                combined_path = text.split(" ", 2)[-1]
                old_path = new_path = _strip_side_prefix(combined_path)
            current_file = FileDiff(
                index=index,
                old_path=old_path,
                new_path=new_path,
                is_combined=is_combined,
            )
            current_file.header_lines.append(physical)
            files.append(current_file)
            file_raw.append(physical)
            continue

        if current_file is None:
            if text.startswith("--- "):
                current_file = FileDiff(
                    index=len(files),
                    old_path=_header_path(text[4:]),
                    new_path=None,
                )
                current_file.header_lines.append(physical)
                files.append(current_file)
                file_raw.append(physical)
            else:
                preamble.append(physical)
            continue

        file_raw.append(physical)

        hunk_match = _HUNK_RE.match(text)
        if hunk_match:
            finish_hunk()
            (
                old_start,
                old_count_text,
                new_start,
                new_count_text,
                suffix,
            ) = hunk_match.groups()
            hunk_index = len(current_file.hunks)
            path = current_file.path
            current_hunk = Hunk(
                path=path,
                old_path=current_file.old_path,
                new_path=current_file.new_path,
                old_start=int(old_start),
                old_count=(int(old_count_text) if old_count_text is not None else 1),
                new_start=int(new_start),
                new_count=(int(new_count_text) if new_count_text is not None else 1),
                header=(suffix or "").strip(),
                raw_header=text,
                header_ending=ending,
                hunk_id=(f"{path}:{int(old_start)}:{int(new_start)}:h{hunk_index:03d}"),
            )
            current_file.hunks.append(current_hunk)
            old_cursor = current_hunk.old_start
            new_cursor = current_hunk.new_start
            old_seen = new_seen = 0
            continue

        if current_hunk is not None:
            if text.startswith("\\ No newline at end of file"):
                kind: LineKind = "meta"
                old_line = new_line = None
            elif text.startswith("+"):
                kind = "add"
                old_line = None
                new_line = new_cursor
                new_cursor += 1
                new_seen += 1
            elif text.startswith("-"):
                kind = "delete"
                old_line = old_cursor
                new_line = None
                old_cursor += 1
                old_seen += 1
            elif text.startswith(" "):
                kind = "context"
                old_line = old_cursor
                new_line = new_cursor
                old_cursor += 1
                new_cursor += 1
                old_seen += 1
                new_seen += 1
            else:
                if strict and (
                    old_seen != current_hunk.old_count or new_seen != current_hunk.new_count
                ):
                    raise DiffParseError(
                        f"invalid line inside hunk {current_hunk.hunk_id}: {text!r}"
                    )
                finish_hunk()
                current_file.trailing_lines.append(physical)
                continue
            line_index = len(current_hunk.lines)
            current_hunk.lines.append(
                DiffLine(
                    kind=kind,
                    content=text[1:] if kind != "meta" else text,
                    old_line=old_line,
                    new_line=new_line,
                    raw=text,
                    ending=ending,
                    source_id=(
                        f"f{current_file.index:04d}:"
                        f"h{len(current_file.hunks) - 1:04d}:l{line_index:06d}"
                    ),
                )
            )
            if strict and (old_seen > current_hunk.old_count or new_seen > current_hunk.new_count):
                raise DiffParseError(f"hunk {current_hunk.hunk_id} exceeds its declared range")
            continue

        current_file.header_lines.append(physical)
        if text.startswith("new file mode "):
            current_file.status = "added"
        elif text.startswith("deleted file mode "):
            current_file.status = "deleted"
        elif text.startswith(("rename from ", "rename to ", "similarity index ")):
            current_file.status = "renamed"
            if text.startswith("rename from "):
                current_file.old_path = _decode_git_quoted(text[len("rename from ") :])
            elif text.startswith("rename to "):
                current_file.new_path = _decode_git_quoted(text[len("rename to ") :])
        elif text.startswith(("copy from ", "copy to ")):
            current_file.status = "copied"
            if text.startswith("copy from "):
                current_file.old_path = _decode_git_quoted(text[len("copy from ") :])
            else:
                current_file.new_path = _decode_git_quoted(text[len("copy to ") :])
        elif text.startswith("--- "):
            current_file.old_path = _header_path(text[4:])
        elif text.startswith("+++ "):
            current_file.new_path = _header_path(text[4:])
        elif text.startswith("Binary files ") or text == "GIT binary patch":
            current_file.is_binary = True

    finish_file()
    parsed = ParsedDiff(raw=raw, preamble="".join(preamble), files=files)
    if strict and (parsed.preamble + "".join(item.raw_text for item in parsed.files) != raw):
        raise DiffParseError("internal parser coverage error: raw patch bytes were not preserved")
    return parsed


def _render_candidate(file_header: str, segments: list[DiffSegment], trailer: str = "") -> str:
    return file_header + "".join(segment.render_annotated() for segment in segments) + trailer


def _segments_for_hunk(file_diff: FileDiff, hunk: Hunk, max_chars: int) -> list[DiffSegment]:
    # A candidate segment's annotated rendering is the hunk header plus each
    # line's annotated rendering, so its length can be tracked incrementally:
    # re-rendering the accumulated candidate for every appended line would be
    # quadratic in hunk size. Each line's annotated length is computed once.
    file_header_length = sum(len(header_line) for header_line in file_diff.header_lines)
    hunk_header_length = len(hunk.raw_header) + len(hunk.header_ending)
    segments: list[DiffSegment] = []
    current: list[DiffLine] = []
    current_length = hunk_header_length

    def emit() -> None:
        nonlocal current_length
        if not current:
            return
        index = len(segments)
        segments.append(
            DiffSegment(
                segment_id=(f"{hunk.hunk_id}:s{index:04d}-{_stable_id(hunk.hunk_id, index)}"),
                hunk_id=hunk.hunk_id,
                path=hunk.path,
                segment_index=index,
                raw_header=hunk.raw_header,
                header_ending=hunk.header_ending,
                lines=tuple(current),
            )
        )
        current.clear()
        current_length = hunk_header_length

    for line in hunk.lines:
        line_length = len(line.render_annotated())
        if file_header_length + current_length + line_length > max_chars:
            emit()
            if file_header_length + current_length + line_length > max_chars:
                raise DiffChunkingError(
                    f"one diff line in {hunk.hunk_id} cannot fit "
                    f"max_chars={max_chars}; increase the cap (the line was not truncated)"
                )
        current.append(line)
        current_length += line_length
    emit()
    return segments


def chunk_diff(parsed: ParsedDiff, *, max_chars: int, include_excluded: bool = False) -> ChunkPlan:
    """Partition eligible patch payload lines without omission or duplication.

    A chunk always contains one file. Oversized hunks become consecutive
    segments, each with the original hunk header and a globally unique segment
    identifier. The cap applies to the annotated, model-facing text.
    """

    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    chunks: list[DiffChunk] = []
    exclusions: list[FileExclusion] = []
    expected: list[str] = []

    for file_diff in parsed.files:
        if file_diff.exclusion is not None:
            # The typed exclusion record is reported even when the excluded
            # file's hunks are chunked anyway under ``include_excluded``.
            exclusions.append(file_diff.exclusion)
            if not include_excluded:
                continue
        if not file_diff.hunks:
            continue
        file_header = "".join(file_diff.header_lines)
        if len(file_header) >= max_chars:
            raise DiffChunkingError(
                f"file header for {file_diff.path} cannot fit max_chars={max_chars}"
            )
        file_segments: list[DiffSegment] = []
        for hunk in file_diff.hunks:
            file_segments.extend(_segments_for_hunk(file_diff, hunk, max_chars))
            expected.extend(line.source_id for line in hunk.lines)

        pending: list[DiffSegment] = []
        file_chunk_index = 0

        def emit_pending(
            *,
            trailer: str = "",
            pending_rows: list[DiffSegment] = pending,
            current_file: FileDiff = file_diff,
            header: str = file_header,
        ) -> None:
            nonlocal file_chunk_index
            if not pending_rows and not trailer:
                return
            digest = _stable_id(
                current_file.path,
                file_chunk_index,
                *(segment.segment_id for segment in pending_rows),
            )
            chunk = DiffChunk(
                chunk_id=(f"f{current_file.index:04d}:c{file_chunk_index:04d}-{digest}"),
                path=current_file.path,
                file_index=current_file.index,
                chunk_index=file_chunk_index,
                file_header=header,
                segments=tuple(pending_rows),
                trailer=trailer,
                max_chars=max_chars,
            )
            if chunk.char_count > max_chars:
                raise DiffChunkingError(
                    f"internal chunk overflow for {current_file.path}: "
                    f"{chunk.char_count}>{max_chars}"
                )
            chunks.append(chunk)
            file_chunk_index += 1
            pending_rows.clear()

        for segment in file_segments:
            if len(_render_candidate(file_header, [*pending, segment])) <= max_chars:
                pending.append(segment)
            else:
                emit_pending()
                pending.append(segment)

        trailer = "".join(file_diff.trailing_lines)
        if trailer and len(_render_candidate(file_header, pending, trailer)) > max_chars:
            emit_pending()
            if len(file_header + trailer) > max_chars:
                raise DiffChunkingError(
                    f"trailing metadata for {file_diff.path} cannot fit max_chars={max_chars}"
                )
        emit_pending(trailer=trailer)

    plan = ChunkPlan(
        chunks=tuple(chunks),
        exclusions=tuple(exclusions),
        expected_source_line_ids=tuple(expected),
        max_chars=max_chars,
        total_files=len(parsed.files),
        eligible_files=sum(1 for item in parsed.files if item.exclusion is None),
    )
    plan.require_complete()
    return plan


__all__ = [
    "ChunkPlan",
    "DiffChunk",
    "DiffChunkingError",
    "DiffLine",
    "DiffParseError",
    "DiffSegment",
    "FileDiff",
    "FileExclusion",
    "Hunk",
    "ParsedDiff",
    "chunk_diff",
    "parse_unified_diff",
]
