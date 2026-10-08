import json
import tempfile
import unittest
from pathlib import Path

from support import TOOL_ROOT, cli, git


class TwoHostGitSyncTest(unittest.TestCase):
    def test_clone_checkpoint_push_pull_resume(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            base = Path(directory)
            remote = base / "remote.git"
            remote.mkdir()
            git(remote, "init", "--bare", "--initial-branch=main")
            a = base / "computer-a"
            git(base, "clone", str(remote), str(a))
            git(a, "config", "user.name", "Codex Sync Test")
            git(a, "config", "user.email", "codex-sync-test@example.invalid")
            (a / "README.md").write_text("project\n", encoding="utf-8")
            git(a, "add", "README.md")
            git(a, "commit", "--no-gpg-sign", "-m", "initial")
            git(a, "push", "-u", "origin", "main")
            cli(a, "init")
            cli(a, "checkpoint", "--goal", "跨电脑开发", "--summary", "A 已完成第一阶段", "--decision", "采用项目级上下文", "--todo", "B 继续验证")
            project_id = json.loads((a / ".codex-sync" / "project.json").read_text(encoding="utf-8"))["project_id"]
            git(a, "add", ".codex-sync")
            git(a, "commit", "--no-gpg-sign", "-m", "checkpoint A")
            git(a, "push", "origin", "main")
            b = base / "computer-b"
            git(base, "clone", str(remote), str(b))
            output = cli(b, "resume").stdout
            for expected in (project_id, "采用项目级上下文", "B 继续验证", "A 已完成第一阶段"):
                self.assertIn(expected, output)
            cli(a, "checkpoint", "--summary", "A 新增第二阶段", "--todo", "B 拉取最新")
            git(a, "add", ".codex-sync")
            git(a, "commit", "--no-gpg-sign", "-m", "checkpoint A again")
            git(a, "push", "origin", "main")
            self.assertIn("Fast-forwarded", cli(b, "sync", "--pull").stdout)
            self.assertIn("A 新增第二阶段", cli(b, "resume").stdout)
            cli(a, "checkpoint", "--summary", "A 第三阶段")
            git(a, "add", ".codex-sync")
            git(a, "commit", "--no-gpg-sign", "-m", "checkpoint A third")
            git(a, "push", "origin", "main")
            (b / "README.md").write_text("local B work\n", encoding="utf-8")
            denied = cli(b, "sync", "--pull", success=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("Working tree is not clean", denied.stderr)
            self.assertEqual((b / "README.md").read_text(encoding="utf-8"), "local B work\n")
