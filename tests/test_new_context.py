from __future__ import annotations

import re
import unittest
from collections.abc import Iterable

from bugbunny.context import ContextBuilder
from bugbunny.diff import parse_unified_diff
from bugbunny.engine import _generation_batches
from bugbunny.models import ReviewConfig
from bugbunny.repository import GrepHit


class FakeSnapshot:
    base_sha = "b" * 40
    head_sha = "h" * 40

    def __init__(self, base: dict[str, str], head: dict[str, str]):
        self.trees = {self.base_sha: base, self.head_sha: head}
        self.grep_calls: list[tuple[str, str, tuple[str, ...] | None]] = []

    def list_files(self, revision: str) -> list[str]:
        return sorted(self.trees[revision])

    def read_text(self, path: str, *, max_bytes: int = 2_000_000) -> str:
        value = self.trees[self.head_sha][path]
        if len(value.encode()) > max_bytes:
            raise ValueError("too large")
        return value

    def read_blob(self, revision: str, path: str, *, max_bytes: int = 2_000_000) -> str:
        value = self.trees[revision][path]
        if len(value.encode()) > max_bytes:
            raise ValueError("too large")
        return value

    def git_grep(
        self,
        pattern: str,
        *,
        revision: str | None = None,
        limit: int = 20,
        fixed: bool = True,
        word: bool = False,
        paths: Iterable[str] | None = None,
        timeout: int = 15,
    ) -> tuple[GrepHit, ...]:
        revision = revision or self.head_sha
        allowed = tuple(sorted(paths)) if paths is not None else None
        self.grep_calls.append((revision, pattern, allowed))
        matcher = re.compile(rf"\b{re.escape(pattern)}\b" if word else re.escape(pattern))
        hits: list[GrepHit] = []
        for path, source in sorted(self.trees[revision].items()):
            if allowed is not None and path not in allowed:
                continue
            for line, text in enumerate(source.splitlines(), 1):
                if matcher.search(text):
                    hits.append(GrepHit(path, line, text))
                    if len(hits) >= limit:
                        return tuple(hits)
        return tuple(hits)


class NewContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = {
            "src/service.ts": """import {persist} from './store';
export async function processItems(items) {
  return items;
}
""",
            "src/store.ts": "export async function persist(id) { return db.save(id); }\n",
            "src/caller.ts": "export const run = () => processItems(queue);\n",
            "tests/service.test.ts": "test('items', () => processItems([]));\n",
        }
        self.head = {
            **self.base,
            "src/service.ts": """import {persist} from './store';
export async function processItems(items) {
  items.forEach(async (item) => {
    await persist(item.id);
  });
}
""",
        }
        self.diff = parse_unified_diff(
            """diff --git a/src/service.ts b/src/service.ts
--- a/src/service.ts
+++ b/src/service.ts
@@ -1,4 +1,6 @@
 import {persist} from './store';
 export async function processItems(items) {
-  return items;
+  items.forEach(async (item) => {
+    await persist(item.id);
+  });
 }
"""
        )

    def test_context_is_per_chunk_bounded_deterministic_and_whole_repo(self) -> None:
        config = ReviewConfig(
            max_chunk_chars=2_000,
            max_context_chars=3_000,
            source_context_lines=4,
            max_symbols_per_chunk=5,
            max_hits_per_symbol=5,
        )
        first_snapshot = FakeSnapshot(self.base, self.head)
        first = ContextBuilder(first_snapshot, config).build(self.diff)  # type: ignore[arg-type]
        second = ContextBuilder(FakeSnapshot(self.base, self.head), config).build(self.diff)  # type: ignore[arg-type]

        self.assertEqual(1, len(first.by_chunk))
        context = next(iter(first.by_chunk.values()))
        self.assertLessEqual(len(context.prompt), config.max_context_chars)
        self.assertEqual(context.prompt, next(iter(second.by_chunk.values())).prompt)
        self.assertEqual("head", context.source.revision if context.source else None)
        self.assertIn("processItems", context.symbols)
        self.assertTrue(any(hit.path == "src/store.ts" for hit in context.definitions))
        self.assertTrue(any(hit.path == "src/caller.ts" for hit in context.callers))
        self.assertTrue(any(hit.path == "tests/service.test.ts" for hit in context.tests))
        self.assertTrue(any(hit.path == "src/service.ts" for hit in context.imports))
        self.assertTrue(first_snapshot.grep_calls)
        self.assertTrue(all(paths is None for _, _, paths in first_snapshot.grep_calls))
        self.assertEqual(
            "repository", "repository" if first.stats["whole_repo_grep_calls"] else "none"
        )
        self.assertTrue(first.stats["chunk_coverage_complete"])

    def test_context_telemetry_measures_only_rendered_model_context(self) -> None:
        config = ReviewConfig(
            max_chunk_chars=2_000,
            max_context_chars=3_000,
            source_context_lines=4,
            max_symbols_per_chunk=5,
            max_hits_per_symbol=5,
        )
        bundle = ContextBuilder(FakeSnapshot(self.base, self.head), config).build(  # type: ignore[arg-type]
            self.diff
        )
        packet = bundle.contexts[0]
        metrics = packet.telemetry
        expected_files = [
            "src/caller.ts",
            "src/service.ts",
            "src/store.ts",
            "tests/service.test.ts",
        ]

        self.assertEqual(expected_files, metrics["context_files_exposed_to_model"])
        self.assertEqual(4, metrics["context_files_exposed_to_model_count"])
        self.assertTrue(metrics["changed_file_context_exposed_to_model"])
        self.assertEqual(
            ["src/caller.ts", "src/store.ts", "tests/service.test.ts"],
            metrics["cross_file_context_files_exposed_to_model"],
        )
        self.assertEqual(expected_files, bundle.stats["context_files_exposed_to_model"])
        self.assertEqual(1, bundle.stats["unique_changed_context_files_exposed_to_model"])
        self.assertEqual(3, bundle.stats["unique_unchanged_context_files_exposed_to_model"])
        self.assertEqual(3, bundle.stats["unique_cross_file_context_files_exposed_to_model"])
        self.assertEqual(len(packet.prompt.encode("utf-8")), metrics["prompt_utf8_bytes"])
        self.assertEqual(metrics["prompt_utf8_bytes"], bundle.stats["context_utf8_bytes"])
        self.assertGreater(metrics["prompt_utf8_bytes"], metrics["prompt_chars"])
        self.assertEqual((len(packet.prompt) + 3) // 4, metrics["estimated_context_tokens"])
        self.assertIn("estimate, not tokenizer output", metrics["estimated_context_tokens_method"])
        self.assertAlmostEqual(
            len(packet.prompt) / metrics["prompt_budget_chars"],
            metrics["prompt_budget_utilization"],
        )
        self.assertEqual(metrics, bundle.stats["packet_metrics"][packet.chunk_id])

    def test_context_telemetry_reports_budget_omissions_and_truncation(self) -> None:
        bundle = ContextBuilder(
            FakeSnapshot(self.base, self.head),
            ReviewConfig(
                max_chunk_chars=2_000,
                max_context_chars=600,
                max_symbols_per_chunk=10,
                max_hits_per_symbol=10,
            ),
        ).build(self.diff)  # type: ignore[arg-type]
        metrics = bundle.contexts[0].telemetry

        self.assertGreater(metrics["symbol_candidates_extracted"], 0)
        self.assertGreater(metrics["symbol_searches_skipped_due_to_budget"], 0)
        self.assertGreater(metrics["evidence_rows_available_to_render"], 0)
        self.assertGreater(metrics["evidence_rows_omitted_due_to_render_budget"], 0)
        self.assertEqual(
            metrics["evidence_rows_available_to_render"],
            metrics["evidence_rows_rendered"]
            + metrics["evidence_rows_omitted_due_to_render_budget"],
        )
        self.assertTrue(metrics["source_excerpt_truncated"])
        self.assertTrue(metrics["prompt_truncated"])
        self.assertEqual(1, bundle.stats["truncated_packets"])
        self.assertEqual(1, bundle.stats["source_truncated_packets"])
        self.assertEqual(1, bundle.stats["prompt_truncated_packets"])
        self.assertEqual(
            metrics["symbol_searches_skipped_due_to_budget"],
            bundle.stats["omission_counts_by_reason"]["symbol_search_budget"],
        )
        self.assertEqual(
            metrics["evidence_rows_omitted_due_to_render_budget"],
            bundle.stats["omission_counts_by_reason"]["evidence_render_budget"],
        )

    def test_symbol_telemetry_distinguishes_config_cap_from_render_budget(self) -> None:
        bundle = ContextBuilder(
            FakeSnapshot(self.base, self.head),
            ReviewConfig(
                max_chunk_chars=2_000,
                max_context_chars=3_000,
                source_context_lines=4,
                max_symbols_per_chunk=2,
                max_hits_per_symbol=5,
            ),
        ).build(self.diff)  # type: ignore[arg-type]
        packet = bundle.contexts[0]
        metrics = packet.telemetry

        # The same top-ranked symbols remain selected; discovery telemetry now
        # makes the earlier hard cap observable instead of hiding it.
        self.assertEqual(("processItems", "persist"), packet.symbols)
        self.assertEqual(4, metrics["symbol_candidates_discovered"])
        self.assertEqual(2, metrics["symbol_candidates_after_config_limit"])
        self.assertEqual(2, metrics["symbol_candidates_extracted"])
        self.assertEqual(2, metrics["symbol_candidates_omitted_by_config_limit"])
        self.assertTrue(metrics["symbol_config_limit_hit"])
        self.assertFalse(metrics["symbol_render_budget_limit_hit"])
        self.assertEqual(["max_symbols_per_chunk"], metrics["symbol_limit_hit_reasons"])
        self.assertEqual(4, bundle.stats["symbol_candidates_discovered"])
        self.assertEqual(2, bundle.stats["symbol_candidates_after_config_limit"])
        self.assertEqual(2, bundle.stats["symbol_candidates_omitted_by_config_limit"])
        self.assertEqual(1, bundle.stats["packets_hitting_symbol_config_limit"])
        self.assertEqual(0, bundle.stats["packets_hitting_symbol_render_budget_limit"])
        self.assertEqual(2, bundle.stats["omission_counts_by_reason"]["symbol_config_limit"])
        self.assertEqual(1, bundle.stats["limit_hit_counts_by_reason"]["max_symbols_per_chunk"])

    def test_async_foreach_is_only_a_labelled_hypothesis(self) -> None:
        context = next(
            iter(
                ContextBuilder(
                    FakeSnapshot(self.base, self.head),
                    ReviewConfig(max_chunk_chars=2_000),
                )
                .build(self.diff)
                .by_chunk.values()  # type: ignore[arg-type]
            )
        )
        self.assertEqual(1, len(context.hypotheses))
        hypothesis = context.hypotheses[0]
        self.assertEqual("hypothesis", hypothesis.status)
        self.assertIn("forEach", hypothesis.cue)
        self.assertIn("HYPOTHESIS", context.prompt)
        self.assertIn("prove", context.prompt)

    def test_deleted_file_context_comes_from_exact_base(self) -> None:
        base = {"src/guard.py": "def run():\n    authorize()\n    mutate()\n"}
        head: dict[str, str] = {}
        parsed = parse_unified_diff(
            """diff --git a/src/guard.py b/src/guard.py
deleted file mode 100644
--- a/src/guard.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def run():
-    authorize()
-    mutate()
"""
        )
        context = next(
            iter(
                ContextBuilder(FakeSnapshot(base, head), ReviewConfig())
                .build(parsed)
                .by_chunk.values()  # type: ignore[arg-type]
            )
        )
        self.assertEqual("base", context.source.revision if context.source else None)
        self.assertIn("authorize", context.source.text if context.source else "")

    def test_excluded_files_are_reported_not_silently_dropped(self) -> None:
        parsed = parse_unified_diff(
            """diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{"v": 1}
+{"v": 2}
"""
        )
        snapshot = FakeSnapshot(
            {"package-lock.json": '{"v": 1}\n'},
            {"package-lock.json": '{"v": 2}\n'},
        )
        bundle = ContextBuilder(snapshot, ReviewConfig()).build(parsed)  # type: ignore[arg-type]
        self.assertEqual({}, bundle.by_chunk)
        self.assertEqual("lockfile", bundle.exclusions[0]["kind"])

    def test_many_small_chunks_fit_the_batch_without_losing_ranked_evidence(
        self,
    ) -> None:
        base: dict[str, str] = {}
        head: dict[str, str] = {}
        patches: list[str] = []
        file_count = 12
        for index in range(file_count):
            source_path = f"src/unit{index}.ts"
            function = f"process{index}"
            persistence = f"persist{index}"
            base[source_path] = (
                f"import {{{persistence}}} from './store{index}';\n"
                f"export async function {function}(items) {{\n"
                "  return items;\n"
                "}\n"
            )
            head[source_path] = (
                f"import {{{persistence}}} from './store{index}';\n"
                f"export async function {function}(items) {{\n"
                "  items.forEach(async (item) => {\n"
                f"    await {persistence}(item.id);\n"
                "  });\n"
                "}\n"
            )
            head[f"src/store{index}.ts"] = base[f"src/store{index}.ts"] = (
                f"export async function {persistence}(id) {{ return db.save(id); }}\n"
            )
            head[f"src/caller{index}.ts"] = base[f"src/caller{index}.ts"] = (
                f"export const run{index} = () => {function}(queue);\n"
            )
            test_path = f"tests/unit{index}.test.ts"
            head[test_path] = base[test_path] = f"test('unit {index}', () => {function}([]));\n"
            patches.append(
                f"""diff --git a/{source_path} b/{source_path}
--- a/{source_path}
+++ b/{source_path}
@@ -1,4 +1,6 @@
 import {{{persistence}}} from './store{index}';
 export async function {function}(items) {{
-  return items;
+  items.forEach(async (item) => {{
+    await {persistence}(item.id);
+  }});
 }}
"""
            )

        parsed = parse_unified_diff("".join(patches))
        config = ReviewConfig(
            max_chunk_chars=36_000,
            max_context_chars=18_000,
            source_context_lines=4,
        )
        snapshot = FakeSnapshot(base, head)
        plan = parsed.chunk(config.max_chunk_chars)
        bundle = ContextBuilder(snapshot, config).build(parsed, plan)  # type: ignore[arg-type]
        batches = _generation_batches(
            plan.chunks,
            {key: value.prompt for key, value in bundle.by_chunk.items()},
            max_patch_chars=config.max_chunk_chars,
            max_context_chars=config.max_context_chars,
        )

        self.assertEqual(1, len(batches))
        self.assertLessEqual(len(batches[0].context), config.max_context_chars)
        self.assertTrue(all(packet.prompt in batches[0].context for packet in bundle.contexts))
        first = bundle.contexts[0]
        self.assertIn("HYPOTHESIS", first.prompt)
        self.assertIn("DEFINITION", first.prompt)
        self.assertIn("CALL SITE", first.prompt)
        self.assertIn("TEST HINT", first.prompt)
        self.assertNotIn("[context packet truncated", first.prompt)
        self.assertLessEqual(len(snapshot.grep_calls), file_count * 3)
        self.assertGreater(bundle.stats["budget_skipped_searches"], 0)

    def test_sub_header_budget_skips_repository_work(self) -> None:
        snapshot = FakeSnapshot(self.base, self.head)
        bundle = ContextBuilder(
            snapshot,
            ReviewConfig(max_chunk_chars=2_000, max_context_chars=160),
        ).build(self.diff)  # type: ignore[arg-type]
        context = bundle.contexts[0]

        self.assertLessEqual(len(context.prompt), bundle.stats["largest_prompt_budget"])
        self.assertIsNone(context.source)
        self.assertEqual([], snapshot.grep_calls)
        self.assertEqual(0, bundle.stats["tree_files"])
        self.assertGreater(bundle.stats["budget_skipped_searches"], 0)
        self.assertEqual(1, bundle.stats["source_reads_skipped_due_to_budget"])
        self.assertEqual(1, bundle.stats["truncated_packets"])
        self.assertEqual([], bundle.stats["context_files_exposed_to_model"])
        self.assertTrue(context.telemetry["source_read_skipped_due_to_budget"])
        self.assertEqual(
            context.telemetry["prompt_chars"], context.telemetry["prompt_budget_chars"]
        )


if __name__ == "__main__":
    unittest.main()
