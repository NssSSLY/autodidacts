# 文件职责：作为 EXE 入口分离用户配置与程序资源；默认启动本地工作台。
"""安装后程序入口；用户配置始终保留在打包目录之外。"""

import os
import sys
from pathlib import Path

from autodidact.resources import resource_root


# 功能：独占补齐缺失的个人默认文件，已有.env/策略/计划绝不覆盖，不把用户数据放回程序目录。
def initialize_user_home(destination, assets):
    destination.mkdir(parents=True, exist_ok=True)
    files = [
        ("config/agent.yaml", "config/agent.yaml"),
        (".env.example", ".env"),
        ("config/run_plan.example.json", "config/run_plan.json"),
    ]
    files.extend(
        (f"data/benchmark/{name}", f"data/benchmark/{name}")
        for name in ("sample.json", "reliable_sample.json", "belief_audit.example.json")
    )
    for source, target in files:
        path = destination / target
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            continue
        content = (assets / source).read_bytes()
        try:
            with path.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            pass


# 功能：EXE模式使用个人配置目录，默认只打开计划预览工作台；源码模式直接转交CLI，不自动学习/迁移。
def main():
    if getattr(sys, "frozen", False):
        local_data = os.environ.get("LOCALAPPDATA")
        destination = (Path(local_data) if local_data else Path.home()) / "Autodidact"
        initialize_user_home(destination, resource_root())
        os.chdir(destination)
        if len(sys.argv) == 1:
            sys.argv.extend(["serve", "--plan", "config/run_plan.json"])
    from autodidact.cli import app

    app()


if __name__ == "__main__":
    main()
