#!/usr/bin/env bash
# SeraGo — pull the latest code and apply DB changes on the VM.
# Safe to re-run any time. The scraper picks up new code on its next run.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/serago}"
cd "${APP_DIR}"

git pull --ff-only
./.venv/bin/pip install --quiet -r requirements.txt
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py seed_sources

echo "Updated. (Runs on the next scheduled tick — or force one with:"
echo "  sudo systemctl start serago-scrape.service)"
