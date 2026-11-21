"""Synthetic tests for workspace pre/post snapshots and diff."""

import json
import os
import tempfile
import unittest
from unittest import mock

from obs import workspace as workspace_mod
from obs.cli import main as cli_main
from obs.workspace import WorkspaceError, collect_workspace, diff_snapshots


class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "workspace")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel: str, content: str) -> str:
        path = os.path.join(self.root, *rel.split("/"))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def test_snapshot_records_files(self):
        self.write("a.txt", "hello")
        self.write("sub/b.bin", "xyz")
        snapshot = collect_workspace(self.root, kind="pre", run_id="r1")
        self.assertTrue(snapshot["root_exists"])
        entries = snapshot["entries"]
        self.assertEqual([entry["path"] for entry in entries], ["a.txt", "sub/b.bin"])
        first = entries[0]
        self.assertEqual(first["type"], "file")
        self.assertEqual(first["size"], 5)
        self.assertTrue(first["exists"])
        self.assertEqual(
            first["sha256"],
            "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        self.assertEqual(snapshot["summary"]["files"], 2)
        self.assertEqual(snapshot["summary"]["bytes"], 8)
        self.assertEqual(snapshot["errors"], [])

    def test_ignored_paths(self):
        self.write(".git/config", "x")
        self.write("__pycache__/a.pyc", "x")
        self.write("sub/__pycache__/b.pyc", "x")
        self.write("runs/r1/events.jsonl", "x")
        self.write("a.tmp", "x")
        self.write("b.temp", "x")
        self.write("c.swp", "x")
        self.write("d~", "x")
        self.write("keep.txt", "x")
        snapshot = collect_workspace(self.root, kind="pre", run_id="r1")
        self.assertEqual(
            [entry["path"] for entry in snapshot["entries"]], ["keep.txt"]
        )

    def test_unreadable_file_is_error(self):
        self.write("locked.txt", "secret")
        self.write("ok.txt", "ok")
        real_hash = workspace_mod._sha256_file

        def fake_hash(path):
            if os.path.basename(str(path)) == "locked.txt":
                raise PermissionError("denied")
            return real_hash(path)

        with mock.patch("obs.workspace._sha256_file", side_effect=fake_hash):
            snapshot = collect_workspace(self.root, kind="pre", run_id="r1")
        self.assertEqual([entry["path"] for entry in snapshot["entries"]], ["ok.txt"])
        self.assertEqual(len(snapshot["errors"]), 1)
        error = snapshot["errors"][0]
        self.assertEqual(error["path"], "locked.txt")
        self.assertEqual(error["type"], "error")
        self.assertIsNone(error["sha256"])
        self.assertTrue(error["exists"])
        self.assertIn("PermissionError", error["error"])
        self.assertEqual(snapshot["summary"]["errors"], 1)

    def test_snapshot_does_not_modify_workspace(self):
        path = self.write("a.txt", "hello")
        before = os.stat(path)
        with open(path, "rb") as handle:
            content_before = handle.read()
        collect_workspace(self.root, kind="pre", run_id="r1")
        after = os.stat(path)
        with open(path, "rb") as handle:
            content_after = handle.read()
        self.assertEqual(content_before, content_after)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_size, after.st_size)

    def test_diff_categories(self):
        self.write("a.txt", "a")
        self.write("b.txt", "b")
        self.write("c.txt", "c")
        pre = collect_workspace(self.root, kind="pre", run_id="r1")

        self.write("b.txt", "b-changed")
        os.unlink(os.path.join(self.root, "c.txt"))
        self.write("d.txt", "d")
        post = collect_workspace(self.root, kind="post", run_id="r1")

        diff = diff_snapshots(pre, post)
        self.assertEqual([e["path"] for e in diff["added"]], ["d.txt"])
        self.assertEqual([e["path"] for e in diff["modified"]], ["b.txt"])
        self.assertEqual([e["path"] for e in diff["deleted"]], ["c.txt"])
        self.assertEqual([e["path"] for e in diff["unchanged"]], ["a.txt"])
        self.assertEqual(diff["summary"], {
            "added": 1, "modified": 1, "deleted": 1, "unchanged": 1, "errors": 0,
        })

        modified = diff["modified"][0]
        self.assertTrue(modified["old_hash"].startswith("sha256:"))
        self.assertTrue(modified["new_hash"].startswith("sha256:"))
        self.assertNotEqual(modified["old_hash"], modified["new_hash"])
        self.assertEqual(modified["old_size"], 1)
        self.assertEqual(modified["new_size"], 9)

        self.assertTrue(diff["added"][0]["exists"])
        self.assertFalse(diff["deleted"][0]["exists"])
        self.assertTrue(diff["unchanged"][0]["exists"])

    def test_diff_error_is_not_added_or_deleted(self):
        self.write("locked.txt", "secret")
        real_hash = workspace_mod._sha256_file

        def fake_hash(path):
            if os.path.basename(str(path)) == "locked.txt":
                raise PermissionError("denied")
            return real_hash(path)

        with mock.patch("obs.workspace._sha256_file", side_effect=fake_hash):
            pre = collect_workspace(self.root, kind="pre", run_id="r1")
        post = collect_workspace(self.root, kind="post", run_id="r1")

        diff = diff_snapshots(pre, post)
        self.assertEqual([e["path"] for e in diff["errors"]], ["locked.txt"])
        self.assertIn("PermissionError", diff["errors"][0]["old_error"])
        self.assertIsNone(diff["errors"][0]["new_error"])
        self.assertEqual(diff["added"], [])
        self.assertEqual(diff["deleted"], [])
        self.assertEqual(diff["modified"], [])
        self.assertEqual(diff["summary"]["errors"], 1)

    def test_diff_rejects_mismatch(self):
        self.write("a.txt", "a")
        pre = collect_workspace(self.root, kind="pre", run_id="r1")
        wrong_run = collect_workspace(self.root, kind="post", run_id="r2")
        with self.assertRaises(WorkspaceError):
            diff_snapshots(pre, wrong_run)

        wrong_kind = collect_workspace(self.root, kind="pre", run_id="r1")
        with self.assertRaises(WorkspaceError):
            diff_snapshots(pre, wrong_kind)

        other_root = os.path.join(self.tmp.name, "other")
        os.makedirs(other_root)
        other = collect_workspace(other_root, kind="post", run_id="r1")
        with self.assertRaises(WorkspaceError):
            diff_snapshots(pre, other)

        other_ignore = collect_workspace(
            self.root, kind="post", run_id="r1", evidence_dirs=("evidence",)
        )
        with self.assertRaises(WorkspaceError):
            diff_snapshots(pre, other_ignore)

    def test_missing_root(self):
        missing = os.path.join(self.tmp.name, "gone")
        snapshot = collect_workspace(missing, kind="pre", run_id="r1")
        self.assertFalse(snapshot["root_exists"])
        self.assertEqual(snapshot["entries"], [])
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["summary"]["files"], 0)

    def test_symlink_recorded(self):
        target = self.write("target.txt", "x")
        link = os.path.join(self.root, "link.txt")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted")
        snapshot = collect_workspace(self.root, kind="pre", run_id="r1")
        entry = next(e for e in snapshot["entries"] if e["path"] == "link.txt")
        self.assertEqual(entry["type"], "symlink")
        self.assertEqual(entry["link_target"], target)
        self.assertTrue(entry["sha256"].startswith("sha256:"))
        self.assertEqual(snapshot["summary"]["symlinks"], 1)

    def test_exclude_snapshot_output(self):
        self.write("a.txt", "a")
        out = os.path.join(self.root, "workspace_pre.json")
        snapshot = collect_workspace(
            self.root, kind="pre", run_id="r1", exclude_paths=[out]
        )
        self.assertNotIn(
            "workspace_pre.json", [entry["path"] for entry in snapshot["entries"]]
        )

    def test_cli_snapshot_and_diff(self):
        self.write("a.txt", "a")
        evidence = os.path.join(self.tmp.name, "evidence")
        pre = os.path.join(evidence, "workspace_pre.json")
        post = os.path.join(evidence, "workspace_post.json")
        diff_path = os.path.join(evidence, "workspace_diff.json")

        code = cli_main([
            "snapshot", "--workspace", self.root, "--out", pre,
            "--kind", "pre", "--run-id", "r1",
        ])
        self.assertEqual(code, 0)

        self.write("b.txt", "b")
        code = cli_main([
            "snapshot", "--workspace", self.root, "--out", post,
            "--kind", "post", "--run-id", "r1",
        ])
        self.assertEqual(code, 0)

        code = cli_main([
            "workspace-diff", "--pre", pre, "--post", post, "--out", diff_path,
        ])
        self.assertEqual(code, 0)
        with open(diff_path, encoding="utf-8") as handle:
            diff = json.load(handle)
        self.assertEqual(diff["summary"]["added"], 1)
        self.assertEqual(diff["summary"]["unchanged"], 1)

    def test_cli_snapshot_excludes_own_output(self):
        self.write("a.txt", "a")
        out = os.path.join(self.root, "workspace_pre.json")
        code = cli_main([
            "snapshot", "--workspace", self.root, "--out", out,
            "--kind", "pre", "--run-id", "r1",
        ])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        self.assertNotIn(
            "workspace_pre.json", [entry["path"] for entry in snapshot["entries"]]
        )

    def test_cli_strict_returns_error_code(self):
        self.write("a.txt", "a")
        out = os.path.join(self.tmp.name, "pre.json")
        with mock.patch(
            "obs.workspace._sha256_file", side_effect=PermissionError("denied")
        ):
            code = cli_main([
                "snapshot", "--workspace", self.root, "--out", out,
                "--kind", "pre", "--run-id", "r1", "--strict",
            ])
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
