from __future__ import annotations

import hashlib
import time
import unittest

from bugbunny.diff import DiffChunkingError, DiffParseError, parse_unified_diff


def _crlf_mixed_diff() -> str:
    body = "".join(
        f"+added line {index} with some payload text\r\n"
        f"-removed line {index} with some payload text\r\n"
        f" context line {index} with some payload text\r\n"
        for index in range(12)
    )
    return (
        "diff --git a/src/mixed.py b/src/mixed.py\r\n"
        "index 1111111..2222222 100644\r\n"
        "--- a/src/mixed.py\r\n"
        "+++ b/src/mixed.py\r\n"
        "@@ -1,24 +1,24 @@ def mixed():\r\n" + body
    )


def _uniform_add_diff() -> str:
    lines = "".join(f"+x{index:03d}\n" for index in range(20))
    return (
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -0,0 +1,20 @@\n" + lines
    )


_MULTI_HUNK_DIFF = (
    "diff --git a/multi.c b/multi.c\n"
    "index 3333333..4444444 100644\n"
    "--- a/multi.c\n"
    "+++ b/multi.c\n"
    "@@ -1,3 +1,4 @@\n"
    " int a;\n"
    "-int b;\n"
    "+int bb;\n"
    "+int c;\n"
    " int d;\n"
    "@@ -100,2 +101,3 @@ void tail(void)\n"
    " old_tail();\n"
    "+new_tail();\n"
    " last_line();\n"
    "\\ No newline at end of file\n"
    "@@ -200,1 +202,1 @@\n"
    "-shrink\n"
    "+grow\n"
)

_WIDE_COORDINATE_DIFF = (
    "diff --git a/wide.py b/wide.py\n"
    "--- a/wide.py\n"
    "+++ b/wide.py\n"
    "@@ -123456789,3 +987654321,3 @@\n"
    " ctx one\n"
    "-del two\n"
    "+add two\n"
    " ctx three\n"
    "diff --git a/second.py b/second.py\n"
    "--- a/second.py\n"
    "+++ b/second.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-alpha\n"
    "+beta\n"
    " gamma\n"
)

# Chunk-plan fixtures captured by running the pre-optimization (candidate
# re-rendering) _segments_for_hunk over these exact diffs. Each entry is
# (case, diff key, max_chars, per-chunk (chunk_id, segment_ids, per-segment
# line counts, char_count), sha256 of the concatenated annotated chunks).
# The incremental implementation must reproduce them byte for byte.
_PlanChunks = tuple[tuple[str, tuple[str, ...], tuple[int, ...], int], ...]
_PLAN_FIXTURES: tuple[tuple[str, str, int, _PlanChunks, str], ...] = (
    (
        "mixed_crlf",
        "mixed",
        700,
        (
            ("f0000:c0000-d629d9d380f7", ("src/mixed.py:1:1:h000:s0000-530cb5091d4e",), (9,), 661),
            ("f0000:c0001-4e6e70675cf5", ("src/mixed.py:1:1:h000:s0001-7e4c039a123f",), (9,), 661),
            ("f0000:c0002-7a3e84e83bfb", ("src/mixed.py:1:1:h000:s0002-675bdd8e2bd2",), (9,), 661),
            ("f0000:c0003-c0fbdd66d318", ("src/mixed.py:1:1:h000:s0003-f741221d56ac",), (9,), 667),
        ),
        "a181a0d4c7348e7c6233d277f8faca98b5bbb11eaa5ce26e1702543a8753db65",
    ),
    (
        "mixed_crlf_tight",
        "mixed",
        400,
        (
            ("f0000:c0000-d629d9d380f7", ("src/mixed.py:1:1:h000:s0000-530cb5091d4e",), (4,), 373),
            ("f0000:c0001-4e6e70675cf5", ("src/mixed.py:1:1:h000:s0001-7e4c039a123f",), (4,), 375),
            ("f0000:c0002-7a3e84e83bfb", ("src/mixed.py:1:1:h000:s0002-675bdd8e2bd2",), (4,), 375),
            ("f0000:c0003-c0fbdd66d318", ("src/mixed.py:1:1:h000:s0003-f741221d56ac",), (4,), 373),
            ("f0000:c0004-239543932ff8", ("src/mixed.py:1:1:h000:s0004-86b72bd5644a",), (4,), 375),
            ("f0000:c0005-06c715ee565c", ("src/mixed.py:1:1:h000:s0005-f15c0eeb3d71",), (4,), 375),
            ("f0000:c0006-551a19c90735", ("src/mixed.py:1:1:h000:s0006-c12f2f4c078b",), (4,), 373),
            ("f0000:c0007-5ecc8272820a", ("src/mixed.py:1:1:h000:s0007-cc780769a90e",), (4,), 377),
            ("f0000:c0008-95148e945623", ("src/mixed.py:1:1:h000:s0008-0830cf68c70d",), (4,), 379),
        ),
        "9f5ca679d1e2a69980314c62993c04f8659828374c9f3be2aea5ad4221a2fa47",
    ),
    (
        "exact_boundary",
        "uniform",
        284,
        (
            ("f0000:c0000-aed7710380a4", ("b.txt:0:1:h000:s0000-78b326e7c88b",), (9,), 284),
            ("f0000:c0001-a1335d012245", ("b.txt:0:1:h000:s0001-cc03a9d43d85",), (9,), 284),
            ("f0000:c0002-ae74e3cea751", ("b.txt:0:1:h000:s0002-0a4c9764ba84",), (2,), 116),
        ),
        "8b65e27a11ce3bdb3c78b1321b550c3229979d5c5e5164fe40fe903d11743117",
    ),
    (
        "exact_boundary_minus_one",
        "uniform",
        283,
        (
            ("f0000:c0000-aed7710380a4", ("b.txt:0:1:h000:s0000-78b326e7c88b",), (8,), 260),
            ("f0000:c0001-a1335d012245", ("b.txt:0:1:h000:s0001-cc03a9d43d85",), (8,), 260),
            ("f0000:c0002-ae74e3cea751", ("b.txt:0:1:h000:s0002-0a4c9764ba84",), (4,), 164),
        ),
        "a376804644204f500f0eae060ce87d4664868815df5d054f6618e41d73e8a4be",
    ),
    (
        "multi_hunk_meta",
        "multi",
        320,
        (
            ("f0000:c0000-9fbdb94b6185", ("multi.c:1:1:h000:s0000-29d3db8989de",), (5,), 236),
            ("f0000:c0001-b47b1b925ef3", ("multi.c:100:101:h001:s0000-f7d91a5f87ff",), (4,), 265),
            ("f0000:c0002-a2a3a0ade4b3", ("multi.c:200:202:h002:s0000-d98ea716de5b",), (2,), 159),
        ),
        "f4236f3425d2ff0e575446340cd0abeee8421a68ec0387630b46449b8cd77fcf",
    ),
    (
        "multi_hunk_meta_roomy",
        "multi",
        5_000,
        (
            (
                "f0000:c0000-5fb469f2bd72",
                (
                    "multi.c:1:1:h000:s0000-29d3db8989de",
                    "multi.c:100:101:h001:s0000-f7d91a5f87ff",
                    "multi.c:200:202:h002:s0000-d98ea716de5b",
                ),
                (5, 4, 2),
                482,
            ),
        ),
        "6ce9d657cbfc1c669696ba4c139aa08650b7e44999310f0c82de1fdeba391f0a",
    ),
    (
        "wide_coordinates",
        "wide",
        300,
        (
            (
                "f0000:c0000-b7bc70c66d01",
                ("wide.py:123456789:987654321:h000:s0000-2b8883d99ff7",),
                (4,),
                213,
            ),
            ("f0001:c0000-a21f13e2227e", ("second.py:1:1:h000:s0000-d2d852e7ea69",), (3,), 157),
        ),
        "ce0ff445bfa6e912118130b0c354b750042a539ebc2bbb4afe9699da2d68adf1",
    ),
)


class NewDiffTests(unittest.TestCase):
    def test_parser_preserves_raw_patch_and_annotates_added_side(self) -> None:
        raw = (
            "diff --git a/src/service.py b/src/service.py\r\n"
            "index 1111111..2222222 100644\r\n"
            "--- a/src/service.py\r\n"
            "+++ b/src/service.py\r\n"
            "@@ -10,3 +10,4 @@ def save():\r\n"
            " keep\r\n"
            "-old\r\n"
            "+new\r\n"
            "+extra\r\n"
            " tail\r\n"
        )
        parsed = parse_unified_diff(raw)

        self.assertEqual(raw, parsed.preamble + "".join(file.raw_text for file in parsed.files))
        self.assertEqual({"src/service.py": {11, 12}}, parsed.changed_line_map())
        self.assertEqual("def save():", parsed.hunks[0].header)
        additions = [line for line in parsed.hunks[0].lines if line.kind == "add"]
        self.assertEqual([11, 12], [line.added_line for line in additions])
        plan = parsed.chunk(1_000)
        self.assertIn("R11", plan.chunks[0].annotated_patch)
        self.assertIn("R12", plan.chunks[0].annotated_patch)

    def test_non_lf_control_characters_remain_inside_one_patch_record(self) -> None:
        raw = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -0,0 +1 @@
+a\x0bb\u2028c
"""
        parsed = parse_unified_diff(raw)
        plan = parsed.chunk(2_000)

        addition = next(line for line in parsed.hunks[0].lines if line.kind == "add")
        self.assertEqual("a\x0bb\u2028c", addition.content)
        self.assertTrue(plan.complete)

    def test_unquoted_git_paths_with_spaces_are_parsed_losslessly(self) -> None:
        raw = """diff --git a/src/space name.py b/src/space name.py
index 1111111..2222222 100644
--- a/src/space name.py
+++ b/src/space name.py
@@ -1 +1 @@
-old = 1
+new = 2
"""

        parsed = parse_unified_diff(raw)

        self.assertEqual("src/space name.py", parsed.files[0].old_path)
        self.assertEqual("src/space name.py", parsed.files[0].new_path)
        self.assertEqual({"src/space name.py": {1}}, parsed.changed_line_map())
        self.assertEqual(raw, parsed.files[0].render_raw())
        self.assertTrue(parsed.chunk(2_000).complete)

    def test_oversized_hunk_is_losslessly_segmented_with_repeated_headers(self) -> None:
        body = "".join(f"+const value{index} = compute({index});\n" for index in range(30))
        raw = (
            "diff --git a/src/large.ts b/src/large.ts\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/large.ts\n"
            "@@ -0,0 +1,30 @@\n" + body
        )
        parsed = parse_unified_diff(raw)
        plan = parsed.chunk(420)

        self.assertGreater(len(plan.chunks), 1)
        self.assertTrue(plan.complete)
        self.assertTrue(all(chunk.char_count <= 420 for chunk in plan.chunks))
        segment_ids = [segment_id for chunk in plan.chunks for segment_id in chunk.segment_ids]
        self.assertEqual(len(segment_ids), len(set(segment_ids)))
        self.assertTrue(all("@@ -0,0 +1,30 @@" in chunk.patch for chunk in plan.chunks))
        source_ids = [source_id for chunk in plan.chunks for source_id in chunk.source_line_ids]
        self.assertEqual(len(source_ids), 30)
        self.assertEqual(len(source_ids), len(set(source_ids)))
        recovered = [
            line.raw
            for chunk in plan.chunks
            for segment in chunk.segments
            for line in segment.lines
        ]
        self.assertEqual(body.splitlines(), recovered)

    def test_exclusions_are_explicit_and_retained_in_parsed_files(self) -> None:
        raw = """diff --git a/image.png b/image.png
index 1111111..2222222 100644
Binary files a/image.png and b/image.png differ
diff --git a/vendor/lib.js b/vendor/lib.js
--- a/vendor/lib.js
+++ b/vendor/lib.js
@@ -1 +1 @@
-old()
+new()
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{"v": 1}
+{"v": 2}
diff --git a/src/schema.generated.ts b/src/schema.generated.ts
--- a/src/schema.generated.ts
+++ b/src/schema.generated.ts
@@ -1 +1 @@
-old
+new
diff --git a/src/real.ts b/src/real.ts
--- a/src/real.ts
+++ b/src/real.ts
@@ -1 +1 @@
-old
+new
"""
        parsed = parse_unified_diff(raw)
        plan = parsed.chunk(2_000)

        self.assertEqual(5, len(parsed.files))
        self.assertEqual(
            ["binary", "vendor", "lockfile", "generated"],
            [exclusion.kind for exclusion in plan.exclusions],
        )
        self.assertEqual(["src/real.ts"], [chunk.path for chunk in plan.chunks])
        self.assertTrue(all(exclusion.reason for exclusion in plan.exclusions))
        self.assertEqual(1, plan.eligible_files)

    def test_generated_phrase_in_ordinary_source_does_not_exclude_file(self) -> None:
        parsed = parse_unified_diff(
            """diff --git a/src/report.py b/src/report.py
--- a/src/report.py
+++ b/src/report.py
@@ -1 +1 @@
-    return "manual"
+    return "This report is automatically generated"
"""
        )

        self.assertIsNone(parsed.files[0].exclusion)
        self.assertEqual(["src/report.py"], [chunk.path for chunk in parsed.chunk(2_000).chunks])

    def test_rename_paths_and_deleted_side_map_are_unambiguous(self) -> None:
        parsed = parse_unified_diff(
            """diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
--- a/old.py
+++ b/new.py
@@ -4,2 +4,1 @@
-guard()
 keep()
"""
        )
        self.assertEqual("renamed", parsed.files[0].status)
        self.assertEqual("old.py", parsed.files[0].old_path)
        self.assertEqual("new.py", parsed.files[0].new_path)
        self.assertEqual({"new.py": set()}, parsed.changed_line_map())
        self.assertEqual({"old.py": {4}}, parsed.deleted_line_map())

    def test_malformed_ranges_and_unrepresentable_long_line_fail_closed(self) -> None:
        with self.assertRaises(DiffParseError):
            parse_unified_diff(
                """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
-old
+new
"""
            )
        parsed = parse_unified_diff(
            """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old
+"""
            + "x" * 600
            + "\n"
        )
        with self.assertRaisesRegex(DiffChunkingError, "was not truncated"):
            parsed.chunk(300)

    def test_chunk_plans_match_pre_optimization_fixtures(self) -> None:
        diffs = {
            "mixed": _crlf_mixed_diff(),
            "uniform": _uniform_add_diff(),
            "multi": _MULTI_HUNK_DIFF,
            "wide": _WIDE_COORDINATE_DIFF,
        }
        for case, diff_key, max_chars, expected_chunks, expected_sha256 in _PLAN_FIXTURES:
            with self.subTest(case=case):
                plan = parse_unified_diff(diffs[diff_key]).chunk(max_chars)
                self.assertTrue(plan.complete)
                observed = tuple(
                    (
                        chunk.chunk_id,
                        chunk.segment_ids,
                        tuple(len(segment.lines) for segment in chunk.segments),
                        chunk.char_count,
                    )
                    for chunk in plan.chunks
                )
                self.assertEqual(expected_chunks, observed)
                self.assertEqual(
                    list(plan.expected_source_line_ids),
                    [
                        source_id
                        for chunk in plan.chunks
                        for source_id in chunk.source_line_ids
                    ],
                )
                annotated = "".join(chunk.annotated_patch for chunk in plan.chunks)
                digest = hashlib.sha256(annotated.encode("utf-8")).hexdigest()
                self.assertEqual(expected_sha256, digest)

    def test_large_hunk_is_segmented_without_quadratic_re_rendering(self) -> None:
        total = 5_000
        body = "".join(f"+value_{index:05d} = compute({index})\n" for index in range(total))
        raw = (
            "diff --git a/big.py b/big.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/big.py\n"
            f"@@ -0,0 +1,{total} @@\n" + body
        )
        parsed = parse_unified_diff(raw)

        started = time.monotonic()
        plan = parsed.chunk(400_000)
        elapsed = time.monotonic() - started

        self.assertTrue(plan.complete)
        self.assertEqual(1, len(plan.chunks))
        self.assertEqual(total, len(plan.chunks[0].source_line_ids))
        # Not a strict timing assertion: the old candidate re-rendering was
        # quadratic and took minutes here; the incremental plan is linear.
        self.assertLess(elapsed, 30.0)

    def test_out_of_range_octal_escape_in_quoted_path_stays_literal(self) -> None:
        raw = (
            'diff --git "a/we\\777ird\\303\\251.py" "b/we\\777ird\\303\\251.py"\n'
            '--- "a/we\\777ird\\303\\251.py"\n'
            '+++ "b/we\\777ird\\303\\251.py"\n'
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        parsed = parse_unified_diff(raw)

        # \777 exceeds one byte, so it stays literal text; \303\251 decodes.
        self.assertEqual("we\\777irdé.py", parsed.files[0].old_path)
        self.assertEqual("we\\777irdé.py", parsed.files[0].new_path)
        self.assertTrue(parsed.chunk(2_000).complete)

    def test_include_excluded_keeps_typed_record_for_chunked_vendor_file(self) -> None:
        raw = """diff --git a/vendor/lib.js b/vendor/lib.js
--- a/vendor/lib.js
+++ b/vendor/lib.js
@@ -1 +1 @@
-old()
+new()
"""
        plan = parse_unified_diff(raw).chunk(2_000, include_excluded=True)

        self.assertEqual(["vendor/lib.js"], [chunk.path for chunk in plan.chunks])
        self.assertTrue(plan.complete)
        self.assertEqual(
            [("vendor/lib.js", "vendor")],
            [(exclusion.path, exclusion.kind) for exclusion in plan.exclusions],
        )

    def test_rename_headers_round_trip_filenames_with_trailing_blanks(self) -> None:
        raw = (
            "diff --git a/old name  b/new name \n"
            "similarity index 100%\n"
            "rename from old name \n"
            "rename to new name \n"
        )
        parsed = parse_unified_diff(raw)

        self.assertEqual("renamed", parsed.files[0].status)
        self.assertEqual("old name ", parsed.files[0].old_path)
        self.assertEqual("new name ", parsed.files[0].new_path)
        self.assertEqual(raw, parsed.files[0].render_raw())


if __name__ == "__main__":
    unittest.main()
