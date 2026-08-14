#!/usr/bin/env bash
# =============================================================================
# SeraGo — one-shot setup for the scheduled scraper on a free Ubuntu VM.
#
# Installs the app, wires the shared Neon DB, and schedules `manage.py
# scrape_all` with a systemd timer (twice a day by default — see
# deploy/serago-scrape.timer for the schedule and how to switch to every
# 30 minutes). Idempotent: safe to re-run after pulling updates or fixing
# .env.
#
# Usage (run as the app user, e.g. `ubuntu`, on the VM):
#   bash setup_vm.sh                 # defaults below
#   WITH_ADMIN=1 bash setup_vm.sh    # also serve the Django admin dashboard
#
# Overridable env vars:
#   REPO_URL    git URL of the repo (default: the GitHub remote)
#   APP_DIR     install directory (default: ~/serago)
#   APP_USER    OS user that owns the app (default: current user)
#   TIMEZONE    VM timezone (default: Africa/Addis_Ababa — the schedule below
#               is written in local time, so keep this)
#   WITH_ADMIN  1 to also run the admin dashboard (gunicorn, port 8000)
#   GUNICORN_WORKERS  gunicorn workers for the admin (default: 2; use 1 on
#               a small 1 GB VM like GCP e2-micro)
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/nebiyuu1954/SeraGo-Scraper.git}"
APP_DIR="${APP_DIR:-$HOME/serago}"
APP_USER="${APP_USER:-$(id -un)}"
TIMEZONE="${TIMEZONE:-Africa/Addis_Ababa}"
WITH_ADMIN="${WITH_ADMIN:-0}"

echo "==> [1/8] System packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl

echo "==> [2/8] Timezone: ${TIMEZONE}"
sudo timedatectl set-timezone "${TIMEZONE}"

echo "==> [3/8] App code -> ${APP_DIR}"
if [ ! -d "${APP_DIR}/.git" ]; then
    sudo mkdir -p "$(dirname "${APP_DIR}")"
    sudo git clone "${REPO_URL}" "${APP_DIR}"
    sudo chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
else
    git -C "${APP_DIR}" pull --ff-only
fi
cd "${APP_DIR}"

echo "==> [4/8] Virtualenv + dependencies"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

echo "==> [5/8] .env secrets"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    Created ${APP_DIR}/.env from the template."
    echo "    >>> PASTE YOUR SECRETS into it (Neon DB_* + ETHIOJOBS_TOKEN),"
    echo "        then run this script again. <<<"
    exit 0
fi

# Required vars must have a non-empty value.
REQUIRED_VARS=(DB_ENGINE DB_NAME DB_USER DB_PASSWORD DB_HOST)
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    if ! grep -qE "^${var}=.+" .env; then
        MISSING+=("${var}")
    fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "ERROR: .env is missing values for: ${MISSING[*]}" >&2
    echo "       Copy them from your local .env (Neon) and re-run this script." >&2
    exit 1
fi

# Replace the placeholder secret key with a real one (needed by the admin).
./.venv/bin/python - <<'PY'
import re
from pathlib import Path
from django.core.management.utils import get_random_secret_key

p = Path(".env")
text = p.read_text()
has_placeholder = bool(re.search(r"^DJANGO_SECRET_KEY\s*=\s*change-me", text, re.M))
has_real = bool(re.search(r"^DJANGO_SECRET_KEY=.+", text, re.M))
if has_placeholder or not has_real:
    key = get_random_secret_key()
    if re.search(r"^DJANGO_SECRET_KEY=", text, re.M):
        text = re.sub(r"(?m)^DJANGO_SECRET_KEY=.*", f"DJANGO_SECRET_KEY={key}", text)
    else:
        text = text.rstrip("\n") + f"\nDJANGO_SECRET_KEY={key}\n"
    p.write_text(text)
    print("    Generated a real DJANGO_SECRET_KEY.")
PY

echo "==> [6/8] Migrations + source seeding"
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py seed_sources

echo "==> [7/8] Scraper timer (systemd)"
sed -e "s|__APP_USER__|${APP_USER}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    "${APP_DIR}/deploy/serago-scrape.service" \
    | sudo tee /etc/systemd/system/serago-scrape.service > /dev/null
sudo cp "${APP_DIR}/deploy/serago-scrape.timer" /etc/systemd/system/serago-scrape.timer
sudo systemctl daemon-reload
sudo systemctl enable --now serago-scrape.timer

if [ "${WITH_ADMIN}" = "1" ]; then
    echo "==> [7b/8] Admin dashboard (gunicorn, port 8000)"
    ./.venv/bin/pip install --quiet gunicorn
    ./.venv/bin/python manage.py collectstatic --noinput
    # e2-micro (GCP free tier, 1 GB RAM): set GUNICORN_WORKERS=1.
    GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
    sed -e "s|__APP_USER__|${APP_USER}|g" \
        -e "s|__APP_DIR__|${APP_DIR}|g" \
        -e "s|__GUNICORN_WORKERS__|${GUNICORN_WORKERS}|g" \
        "${APP_DIR}/deploy/serago-admin.service" \
        | sudo tee /etc/systemd/system/serago-admin.service > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now serago-admin.service
    PUBLIC_IP="$(curl -s --max-time 10 https://ifconfig.me || echo 'YOUR_PUBLIC_IP')"
    echo "    Add this IP to DJANGO_ALLOWED_HOSTS in .env: ${PUBLIC_IP}"
    echo "    (then: sudo systemctl restart serago-admin)"
fi

echo "==> [8/8] Smoke test: one scrape_all run now"
set +e
./.venv/bin/python manage.py scrape_all
SCRAPE_RC=$?
set -e
if [ "${SCRAPE_RC}" -ne 0 ]; then
    echo "    scrape_all finished with exit ${SCRAPE_RC} (some sources failed — see the log above)."
else
    echo "    scrape_all finished successfully."
fi

echo
echo "=============================================================="
echo " Done. Current schedule:"
systemctl list-timers serago-scrape.timer --no-pager
echo
echo " Logs:          journalctl -u serago-scrape -e"
echo " Day report:    ${APP_DIR}/.venv/bin/python manage.py log_report"
if [ "${WITH_ADMIN}" = "1" ]; then
    echo " Admin:         http://$(curl -s --max-time 10 https://ifconfig.me || echo '<public-ip>'):8000/admin/"
    echo " First login:   ${APP_DIR}/.venv/bin/python manage.py createsuperuser"
fi
echo
echo " Switch to every 30 minutes (one edit):"
echo "   sudo nano /etc/systemd/system/serago-scrape.timer"
echo "   change OnCalendar to:  OnCalendar=*-*-* *:00,30:00"
echo "   then: sudo systemctl daemon-reload && sudo systemctl restart serago-scrape.timer"
echo "=============================================================="
