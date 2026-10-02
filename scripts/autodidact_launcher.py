# 文件职责：作为 EXE 入口分离用户配置与程序资源；默认启动本地工作台。
"""Installed program entry point; user configuration stays outside the bundle."""

import os
import shutil
import sys
from pathlib import Path

from autodidact.resources import resource_root


# 功能：EXE 模式只补不存在的用户配置并切换工作目录，无参数时运行 serve；源码模式直接转交 CLI。
def main():
    if getattr(sys, "frozen", False):
        destination = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Autodidact"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "config").mkdir(exist_ok=True)
        assets = resource_root()
        for source, target in [
            (assets / "config" / "agent.yaml", destination / "config" / "agent.yaml"),
            (assets / ".env.example", destination / ".env"),
        ]:
            if not target.exists():
                shutil.copy2(source, target)
        os.chdir(destination)
        if len(sys.argv) == 1:
            sys.argv.append("serve")
    from autodidact.cli import app

    app()


if __name__ == "__main__":
    main()
