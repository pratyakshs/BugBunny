from __future__ import annotations

import unittest

from bugbunny.diff import DiffChunkingError, DiffParseError, parse_unified_diff


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


if __name__ == "__main__":
    unittest.main()
