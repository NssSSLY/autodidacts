#!/usr/bin/env bash
# 文件职责：类 Unix 开发环境快捷准备：启动示例数据库，安装开发依赖/Chromium并初始化；不是正式服务部署。
set -euo pipefail
[ -f .env ] || cp .env.example .env
docker compose up -d
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium
.venv/bin/autodidact init-db
.venv/bin/autodidact bootstrap
echo "Ready. Edit .env, then run: .venv/bin/autodidact run-once"
