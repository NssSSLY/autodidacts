"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import checkpoint, git, project, resume, status


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="codex-sync", description="通过 Git 保存和恢复 Codex 项目上下文")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化可移植的项目上下文，并保留 V1 文件")
    commands.add_parser("status", help="显示项目、Git 和检查点状态")
    capture = commands.add_parser("checkpoint", help="记录一次明确提供的项目检查点")
    capture.add_argument("--goal", default="")
    capture.add_argument("--summary", default="")
    capture.add_argument("--decision", action="append", default=[])
    capture.add_argument("--todo", action="append", default=[])
    capture.add_argument("--blocker", action="append", default=[])
    commands.add_parser("resume", help="为新聊天输出结构化的项目上下文")
    sync = commands.add_parser("sync", help="获取并检查上游；可选择快进拉取")
    sync.add_argument("--pull", action="store_true", help="仅在工作区干净时快进拉取")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = git.repo_root(Path.cwd())
        if args.command == "init":
            data = project.initialize(root)
            print(f"已初始化 {project.sync_dir(root)}（项目 ID：{data['project_id']}）")
            legacy_index = git.run(root, "ls-files", "--", ".codex-sync/decisions.md", ".codex-sync/current-state.md")
            if legacy_index:
                print("Git 索引中仍有 V1 小写文件名。准备暂存迁移结果时，请运行：")
                print("  git -c core.ignorecase=false add -A -- .codex-sync")
        elif args.command == "status":
            print(status.render(root))
        elif args.command == "checkpoint":
            path = checkpoint.create(
                root, goal=args.goal, summary=args.summary, decisions=args.decision,
                todos=args.todo, blockers=args.blocker,
            )
            print(f"检查点已保存：{path}")
        elif args.command == "resume":
            print(resume.render(root), end="")
        elif args.command == "sync":
            print(git.sync(root, pull=args.pull))
        return 0
    except (git.SyncError, OSError, ValueError) as error:
        print(f"codex-sync: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
