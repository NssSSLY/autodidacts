import json
import tempfile
import unittest
import uuid
from pathlib import Path

from support import TOOL_ROOT, cli, git, make_repo


class InitTest(unittest.TestCase):
    def test_migrates_v1_without_changing_original_files(self):
        with tempfile.TemporaryDirectory(dir=TOOL_ROOT) as directory:
            root = make_repo(Path(directory))
            sync = root / ".codex-sync"
            sync.mkdir()
            (sync / "decisions.md").write_text("# 旧决策\n\n- 保留旧方案\n", encoding="utf-8")
            (sync / "current-state.md").write_text("# 旧状态\n\n仍在开发\n", encoding="utf-8")
            git(root, "add", ".codex-sync")
            git(root, "commit", "--no-gpg-sign", "-m", "V1 context")
            self.assertIn("core.ignorecase=false", cli(root, "init").stdout)
            metadata = json.loads((sync / "project.json").read_text(encoding="utf-8"))
            uuid.UUID(metadata["project_id"])
            self.assertEqual(metadata["repository_root"], ".")
            self.assertEqual(metadata["schema_version"], 2)
            names = {path.name for path in sync.iterdir()}
            self.assertIn("DECISIONS.md", names)
            self.assertIn("CURRENT_STATE.md", names)
            self.assertNotIn("decisions.md", names)
            self.assertNotIn("current-state.md", names)
            self.assertEqual((sync / "DECISIONS.md").read_text(encoding="utf-8"), (sync / "legacy-v1" / "decisions.md").read_text(encoding="utf-8"))
            self.assertEqual((sync / "CURRENT_STATE.md").read_text(encoding="utf-8"), (sync / "legacy-v1" / "current-state.md").read_text(encoding="utf-8"))
            self.assertTrue((sync / "PROJECT_CONTEXT.md").is_file())
            self.assertTrue((sync / "TODO.md").is_file())
            cli(root, "init")
            self.assertEqual(metadata["project_id"], json.loads((sync / "project.json").read_text(encoding="utf-8"))["project_id"])
            git(root, "-c", "core.ignorecase=false", "add", "-A", ".codex-sync")
            indexed = set(git(root, "ls-files", ".codex-sync").splitlines())
            self.assertIn(".codex-sync/DECISIONS.md", indexed)
            self.assertIn(".codex-sync/CURRENT_STATE.md", indexed)
            self.assertIn(".codex-sync/legacy-v1/decisions.md", indexed)
            self.assertIn(".codex-sync/legacy-v1/current-state.md", indexed)
            self.assertNotIn(".codex-sync/decisions.md", indexed)
            self.assertNotIn(".codex-sync/current-state.md", indexed)
            observed = cli(root, "status", extra_env={"CODEX_HOME": r"E:\Codex\home"}).stdout
            self.assertIn(r"E:\Codex\home", observed)
