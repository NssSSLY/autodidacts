import json
import tempfile
import unittest
from pathlib import Path

from support import TOOL_ROOT, cli, make_repo


class ResumeTest(unittest.TestCase):
    def test_renders_portable_documents_and_latest_checkpoint(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            root = make_repo(Path(directory))
            cli(root, "init")
            sync = root / ".codex-sync"
            (sync / "PROJECT_CONTEXT.md").write_text("# 项目\n\n晋中麻将\n", encoding="utf-8")
            (sync / "TODO.md").write_text("# TODO\n\n- 验证双机恢复\n", encoding="utf-8")
            cli(root, "checkpoint", "--summary", "已完成解析器", "--decision", "使用 Git", "--todo", "验证双机恢复")
            output = cli(root, "resume").stdout
            project_id = json.loads((sync / "project.json").read_text(encoding="utf-8"))["project_id"]
            for expected in ("Codex 项目恢复上下文", project_id, "晋中麻将", "使用 Git", "验证双机恢复", "已完成解析器", "最新检查点", "近期历史"):
                self.assertIn(expected, output)
