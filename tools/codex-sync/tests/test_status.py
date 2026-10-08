import tempfile
import unittest
from pathlib import Path

from support import TOOL_ROOT, cli, make_repo


class StatusTest(unittest.TestCase):
    def test_reports_branch_head_dirty_and_latest_checkpoint(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            root = make_repo(Path(directory))
            cli(root, "init")
            before = cli(root, "status").stdout
            self.assertIn("分支：main", before)
            self.assertIn("未提交改动：有", before)
            self.assertIn("最新检查点：无", before)
            cli(root, "checkpoint", "--summary", "第一阶段完成")
            after = cli(root, "status").stdout
            self.assertIn("最新检查点：20", after)
