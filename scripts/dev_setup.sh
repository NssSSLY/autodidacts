#!/usr/bin/env bash
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
