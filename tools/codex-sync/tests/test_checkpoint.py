import json
import tempfile
import unittest
from pathlib import Path

from support import TOOL_ROOT, cli, make_repo


class CheckpointTest(unittest.TestCase):
    def test_collects_git_metadata_and_explicit_context_without_secret_file(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            root = make_repo(Path(directory))
            cli(root, "init")
            (root / "README.md").write_text("updated\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=do-not-export\n", encoding="utf-8")
            cli(root, "checkpoint", "--goal", "完成 V2", "--summary", "完成主流程", "--decision", "保留旧文件", "--todo", "双机验证", "--blocker", "等待远端主机")
            sync = root / ".codex-sync"
            checkpoint = next((sync / "checkpoints").glob("*.json"))
            data = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(data["goal"], "完成 V2")
            self.assertEqual(data["summary"], "完成主流程")
            self.assertEqual(data["git_branch"], "main")
            self.assertIn("README.md", data["modified_files"])
            self.assertTrue(data["git_head"])
            self.assertTrue(data["recent_commits"])
            self.assertTrue(data["git_diff_summary"])
            self.assertNotIn(".env", checkpoint.read_text(encoding="utf-8"))
            self.assertNotIn("do-not-export", checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(len(list((sync / "history").glob("*.jsonl"))), 1)
            self.assertEqual(len(list((sync / "checkpoints").glob("*.md"))), 1)

    def test_rejects_supplied_credentials(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            root = make_repo(Path(directory))
            cli(root, "init")
            result = cli(root, "checkpoint", "--summary", "token=abc123", success=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list((root / ".codex-sync" / "checkpoints").glob("*.json")))
