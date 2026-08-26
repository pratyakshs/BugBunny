from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bugbunny.models import PRInfo
from bugbunny.repository import (
    GitRepositoryCache,
    RepositoryError,
    RepositoryLimitError,
    RepositorySafetyError,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def make_repository(root: Path) -> tuple[Path, str, str]:
    repo = root / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "BugBunny test")
    (repo / "src").mkdir()
    (repo / "src" / "changed.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "src" / "unchanged.py").write_text(
        "def whole_repo_symbol():\n    return 1\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "changed.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "commit", "-qam", "head")
    head = git(repo, "rev-parse", "HEAD")
    return repo, base, head


class NewRepositoryTests(unittest.TestCase):
    def test_prepare_reuses_remote_shard_without_materializing_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, head = make_repository(root)
            pr = PRInfo(
                url="https://github.com/fixture/repo/pull/1",
                owner="fixture",
                repo="repo",
                number=1,
                clone_url=str(repo),
                title="fixture",
                body="",
                base_ref="master",
                base_sha=base,
                head_ref="master",
                head_sha=head,
                resolved_at="2026-01-01T00:00:00Z",
            )
            cache = GitRepositoryCache(root / "cache", shard_by_remote=True)

            prepared = cache.prepare(pr)

            self.assertEqual(base, prepared.base_sha)
            self.assertEqual(head, prepared.head_sha)
            self.assertGreater(prepared.diff_bytes, 0)
            self.assertFalse(prepared.cache_hit)
            self.assertEqual([], list((root / "cache").glob("repositories/*/worktrees/*")))

            with cache.acquire(pr) as snapshot:
                self.assertTrue(snapshot.cache_hit)
                self.assertIn("repositories", snapshot.object_dir.parts)
                self.assertEqual(prepared.diff_sha256, snapshot.diff_sha256)

    def test_diverged_base_uses_merge_base_to_reconstruct_the_pr_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, common, _linear_head = make_repository(root)
            git(repo, "branch", "feature", common)
            git(repo, "switch", "-q", "feature")
            (repo / "feature_only.py").write_text("feature = True\n", encoding="utf-8")
            git(repo, "add", "feature_only.py")
            git(repo, "commit", "-qm", "feature change")
            feature_head = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-q", "master")
            (repo / "base_only.py").write_text("base = True\n", encoding="utf-8")
            git(repo, "add", "base_only.py")
            git(repo, "commit", "-qm", "base advanced independently")
            base_tip = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=base_tip, head_sha=feature_head
            ) as snapshot:
                patch = snapshot.diff()
                self.assertEqual(common, snapshot.review_base_sha)
                self.assertIn("feature_only.py", patch)
                self.assertNotIn("base_only.py", patch)

    def test_shallow_fetch_recovers_merge_base_beyond_initial_history_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, common = make_repository(root)
            git(repo, "branch", "feature", common)
            for index in range(3):
                git(repo, "commit", "--allow-empty", "-qm", f"base advance {index}")
            base_tip = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-q", "feature")
            for index in range(3):
                git(repo, "commit", "--allow-empty", "-qm", f"head advance {index}")
            head_tip = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache", history_depth=2).from_local(
                repo, base_sha=base_tip, head_sha=head_tip
            ) as snapshot:
                self.assertEqual(common, snapshot.review_base_sha)
                snapshot.assert_clean()

    def test_shared_exact_object_cache_creates_isolated_clean_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, head = make_repository(root)
            cache = GitRepositoryCache(root / "cache")
            first = cache.from_local(repo, base_sha=base, head_sha=head)
            second = cache.from_local(repo, base_sha=base, head_sha=head)
            try:
                self.assertEqual(first.object_dir, second.object_dir)
                self.assertNotEqual(first.worktree_path, second.worktree_path)
                self.assertFalse(first.cache_hit)
                self.assertTrue(second.cache_hit)
                self.assertEqual("value = 2\n", first.read_text("src/changed.py"))
                self.assertEqual("value = 1\n", first.read_blob(base, "src/changed.py"))
                self.assertIn("+value = 2", first.diff())
                first.assert_clean()
                second.assert_clean()
            finally:
                first.close()
                second.close()
            self.assertFalse(first.worktree_path.exists())
            self.assertFalse(second.worktree_path.exists())

    def test_diff_uses_cache_independent_full_object_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, head = make_repository(root)

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=base, head_sha=head
            ) as snapshot:
                patch = snapshot.diff()
                match = re.search(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", patch, re.MULTILINE)
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(40, len(match.group(1)))
                self.assertEqual(40, len(match.group(2)))

    def test_uninitialized_gitlink_is_materialized_as_an_empty_clean_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, base = make_repository(root)
            git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{base},vendored-repo",
            )
            git(repo, "commit", "-qm", "add gitlink")
            head = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=base, head_sha=head
            ) as snapshot:
                mount = snapshot.worktree_path / "vendored-repo"
                self.assertTrue(mount.is_dir())
                self.assertEqual([], list(mount.iterdir()))
                snapshot.assert_clean()

    def test_reads_and_whole_repository_git_grep_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, head = make_repository(root)
            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=base, head_sha=head
            ) as snapshot:
                hits = snapshot.git_grep("whole_repo_symbol", limit=3, word=True)
                self.assertEqual(1, len(hits))
                self.assertEqual("src/unchanged.py", hits[0].path)
                self.assertEqual(1, hits[0].line)
                with self.assertRaises(RepositoryLimitError):
                    snapshot.read_text("src/unchanged.py", max_bytes=4)
                with self.assertRaises(RepositoryLimitError):
                    snapshot.read_blob(head, "src/unchanged.py", max_bytes=4)
                with self.assertRaises(RepositorySafetyError):
                    snapshot.read_text("../outside")
                with self.assertRaises(RepositorySafetyError):
                    snapshot.read_blob("f" * 40, "src/changed.py")

    def test_git_grep_can_scope_a_path_with_pathspec_metacharacters_literally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, previous_head = make_repository(root)
            (repo / "src" / "[literal].py").write_text("scoped_symbol = 1\n", encoding="utf-8")
            (repo / "src" / "l.py").write_text("scoped_symbol = 2\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "add literal pathspec fixture")
            head = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=previous_head, head_sha=head
            ) as snapshot:
                hits = snapshot.git_grep(
                    "scoped_symbol",
                    paths=("src/[literal].py",),
                    literal_paths=True,
                )

            self.assertEqual(["src/[literal].py"], [hit.path for hit in hits])

    def test_git_grep_matches_containing_control_bytes_do_not_abort_the_search(self) -> None:
        # git grep -z -n terminates records with LF only; a matched line
        # containing \r, \f, or another str.splitlines separator must stay a
        # single record instead of fragmenting and failing the 3-field parse.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, previous_head = make_repository(root)
            (repo / "src" / "control.txt").write_bytes(
                b"first line\nSEARCHME cr\rTAIL\nSEARCHME ff\x0cTAIL2\nlast line\n"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "add control-byte fixture")
            head = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=previous_head, head_sha=head
            ) as snapshot:
                hits = snapshot.git_grep("SEARCHME")

            self.assertEqual(
                [
                    ("src/control.txt", 2, "SEARCHME cr\rTAIL"),
                    ("src/control.txt", 3, "SEARCHME ff\x0cTAIL2"),
                ],
                [(hit.path, hit.line, hit.text) for hit in hits],
            )

    def test_git_grep_accepts_a_matching_line_larger_than_the_old_output_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, previous_head = make_repository(root)
            long_line = "long_line_target " + ("x" * 500_000) + "\n"
            (repo / "src" / "generated.svg").write_text(long_line, encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "add long generated line")
            head = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=previous_head, head_sha=head
            ) as snapshot:
                hits = snapshot.git_grep("long_line_target", limit=3)

            self.assertEqual(1, len(hits))
            self.assertEqual("src/generated.svg", hits[0].path)
            self.assertEqual(1, hits[0].line)
            self.assertEqual(len(long_line.rstrip("\n")), len(hits[0].text))

    def test_symlink_escape_fails_acquisition_and_internal_link_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, head = make_repository(root)
            (repo / "inside-link").symlink_to("src/changed.py")
            git(repo, "add", "inside-link")
            git(repo, "commit", "-qm", "internal symlink")
            internal_head = git(repo, "rev-parse", "HEAD")
            with (
                GitRepositoryCache(root / "cache-a").from_local(
                    repo, base_sha=head, head_sha=internal_head
                ) as snapshot,
                self.assertRaises(RepositorySafetyError),
            ):
                snapshot.read_text("inside-link")

            (repo / "escape-link").symlink_to("../../outside")
            git(repo, "add", "escape-link")
            git(repo, "commit", "-qm", "escaping symlink")
            escaping_head = git(repo, "rev-parse", "HEAD")
            with self.assertRaisesRegex(RepositorySafetyError, "escapes worktree"):
                GitRepositoryCache(root / "cache-b").from_local(
                    repo, base_sha=internal_head, head_sha=escaping_head
                )

    def test_repository_hooks_and_checkout_filters_are_never_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, _, head = make_repository(root)
            sentinel = root / "executed"
            hook = repo / ".git" / "hooks" / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n", encoding="utf-8")
            hook.chmod(0o755)
            (repo / ".gitattributes").write_text("*.py filter=evil\n", encoding="utf-8")
            git(repo, "config", "filter.evil.smudge", f"touch '{sentinel}'; cat")
            git(repo, "add", ".gitattributes")
            git(repo, "commit", "-qm", "hostile checkout configuration")
            hostile_head = git(repo, "rev-parse", "HEAD")

            with GitRepositoryCache(root / "cache").from_local(
                repo, base_sha=head, head_sha=hostile_head
            ) as snapshot:
                self.assertEqual("value = 2\n", snapshot.read_text("src/changed.py"))
            self.assertFalse(sentinel.exists())

    def test_invalid_or_missing_exact_commits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, _ = make_repository(root)
            cache = GitRepositoryCache(root / "cache", command_timeout=5)
            with self.assertRaisesRegex(RepositoryError, "full 40"):
                cache.from_local(repo, base_sha="HEAD", head_sha=base)
            with self.assertRaisesRegex(RepositoryError, "could not fetch exact"):
                cache.from_local(repo, base_sha=base, head_sha="f" * 40)

    def test_worktree_tree_enumeration_is_bounded_before_buffering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, base, head = make_repository(root)
            with (
                patch("bugbunny.repository.DEFAULT_MAX_TREE_BYTES", 64),
                self.assertRaisesRegex(RepositoryLimitError, "tree listing exceeds 64 bytes"),
            ):
                GitRepositoryCache(root / "cache").from_local(repo, base_sha=base, head_sha=head)


if __name__ == "__main__":
    unittest.main()
