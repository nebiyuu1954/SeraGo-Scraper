# SeraGo — Deployment Plan (free, scheduled scraper)

This document is the plan for running SeraGo in production for **$0/month**.
It was written for the current goal — scrape twice a day — and designed so
that switching to **every 30 minutes** later is a one-line change.

---

## 1. What we deploy and how it works

SeraGo is a scheduler-less Django app: it doesn't need to run 24/7. The
scraper wakes up, sweeps all 5 job sites, writes to the shared Neon
PostgreSQL database, and exits. So we don't deploy a web server that stays
up — we deploy a **tiny always-free VM with a cron-like timer** that runs
`manage.py scrape_all` on schedule.

```
┌─────────────────────────────┐
│ Free cloud VM (always free) │   Oracle Cloud A1.Flex or GCP e2-micro
│                             │
│  systemd timer ──▶ serago-scrape.service ──▶ .venv/bin/python
│  (12:00 & 23:30                 (one run)       manage.py scrape_all
│   Addis time)                                      │
│                                                     ▼
│  optional: serago-admin.service ── gunicorn ──▶ Django /admin/ (monitor)
└─────────────────────────────┘
        │ writes
        ▼
┌─────────────────────────────┐
│ Neon PostgreSQL (free tier) │  ← already connected, shared with .NET backend
└─────────────────────────────┘
```

Key choices (and why):

| Decision | Choice | Why |
|---|---|---|
| Scheduler | **systemd timer on a VM** — or **GitHub Actions** if you have no credit card (see §2b) | systemd timer: free forever, reliable, works for every-30-min. GitHub Actions `schedule` is disabled on free **private** repos but works on **public** ones. Celery/Redis would need an always-on process + paid Redis — overkill. |
| VM | **Oracle Cloud Always Free** (primary) / **GCP e2-micro** (backup) | Both genuinely $0/month, no sleep, no time limits. |
| DB | **Neon** (already done) | Shared with the .NET backend; each run only uses a few minutes of the free compute budget. |
| How often | **12:00 & 23:30 Addis Ababa time** | Your choice: mid-day and 30 minutes before midnight. One line to change later. |

The project's `scrape_all` command was already built for this: it runs every
active source, keeps going if one fails, and returns a non-zero exit code on
failure — exactly what a timer wants.

---

## 2. Cost breakdown ($0)

- **VM**: Oracle Cloud Always Free — one Ampere A1.Flex (4 OCPU / 24 GB RAM)
  or 2 × AMD micro. GCP e2-micro as an alternative.
- **Database**: Neon free tier — 100 CU-hours/month (~400 hours at the
  default 0.25 CU compute). Twice-daily runs use roughly **5–10 hours/month**.
  Even at every-30-minutes with the fast incremental runs below, expect
  ~100–150 hours/month — still inside the free budget. Watch the Neon
  dashboard's "Active time" if you go to 30 minutes with slow sites.
- **Everything else**: systemd, gunicorn, whitenoise — free open source.

> Why not GitHub Actions (private repo)? Free personal accounts effectively
> disable `schedule` events on **private** repos (a known limitation), and
> private repos only get 2,000 runner-minutes/month — a 30-minute cadence
> would burn ~7,000. **Public** repos are unlimited. See §2b below.

### 2b. No credit card? Use GitHub Actions (repo goes public)

GCP/Oracle signup asks for a credit or debit card for identity verification,
so a prepaid card often blocks the VM path. If that's the case, the cleanest
free route is GitHub Actions on a **public** repo — no card, no servers:

- The workflow (`.github/workflows/scrape.yml`) is already committed: it runs
  `migrate` → `seed_sources` → `scrape_all` on a cron schedule and reads all
  secrets from GitHub **Actions secrets** (Settings → Secrets → Actions).
- Schedule (cron is UTC; Addis = UTC+3): `0 9 * * *` + `30 20 * * *` =
  12:00 & 23:30 Addis. Switch to every 30 min later by replacing both
  entries with `*/30 * * * *`.
- **To go live:** 1) make the repo public (Settings → General → Danger
  Zone → Change visibility); 2) add the secrets (DB_* from Neon,
  `DJANGO_SECRET_KEY`, `ETHIOJOBS_TOKEN`, optional `JINA_API_KEY`);
  3) push the workflow file — the first run fires on the next schedule or
  via the **Run workflow** button (Actions tab).
- This repo is safe to make public: `.env` was never committed, and the
  only `eyJ...` strings in the repo are EthioJobs' per-request encrypted
  job IDs in fixture data — not tokens. The real `ETHIOJOBS_TOKEN` lives
  only in Actions secrets.
- Caveats: code becomes visible/forkable; scheduled workflows pause after
  60 days with no repo activity (normal commits reset the clock); runs can
  be queued a few minutes late (fine for a scraper).

---

## 3. One-time setup (≈20 minutes)

### Step 1 — Create the free VM

**Option A — Oracle Cloud (recommended, most generous):**

1. Sign up at <https://www.oracle.com/cloud/free/> (free account; a card is
   asked for identity verification but **nothing is charged**).
2. Console → Compute → Instances → **Create instance**:
   - Image: **Canonical Ubuntu 24.04** (ships Python 3.12).
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM), 4 OCPU / 24 GB RAM
     (Always Free eligible).
   - Add your SSH public key (or let Oracle generate a key pair and download
     the private key).
   - Boot volume: 47 GB (free).
3. Note the instance's **public IP**.
4. Security list note: port 22 is open by default. If you later run the admin
   dashboard and want it reachable from the internet, open port 8000 in
   Networking → Security Lists. (Safer alternative: leave it closed and use
   an SSH tunnel.)

**Option B — Google Cloud (e2-micro):**

1. Sign up at <https://cloud.google.com/free> (card needed for identity
   verification; you get a $300 trial credit AND the always-free e2-micro —
   the e2-micro stays free after the trial ends as long as you stay within
   the free limits).
2. Create a project, then enable the **Compute Engine API**.
3. Compute Engine → VM instances → **Create instance**:
   - Name anything; region **us-central1** (or us-east1 / us-west1 — the
     free e2-micro only exists in these three).
   - Machine type: **e2-micro** (free tier; 2 shared vCPU / 1 GB RAM).
   - Boot disk: **Ubuntu 24.04**, size **30 GB**, type **Standard persistent
     disk** (standard disk ≤ 30 GB is what the free tier covers — an SSD
     boot disk would cost money).
4. SSH: use the **SSH button in the console** — no key files to manage.
5. Run the exact same setup script as Oracle:
   `bash <(curl -s https://raw.githubusercontent.com/nebiyuu1954/SeraGo-Scraper/main/deploy/setup_vm.sh)`
6. To reach the admin dashboard from the internet, add a firewall rule
   allowing TCP **8000** (VPC network → Firewall), or just use an SSH tunnel.

   e2-micro notes: 1 GB RAM is plenty for the twice-daily sweep (and even
   every-30-min runs — each run is a short-lived process). If you install the
   admin dashboard too, use `GUNICORN_WORKERS=1`:
   `WITH_ADMIN=1 GUNICORN_WORKERS=1 bash setup_vm.sh`

### Step 2 — Run the setup script (idempotent)

```bash
# SSH into the VM, then:
bash <(curl -s https://raw.githubusercontent.com/nebiyuu1954/SeraGo-Scraper/main/deploy/setup_vm.sh)
```

What it does (each step prints `[n/8]`):

1. Installs `python3`/venv/git/curl.
2. Sets the VM timezone to **Africa/Addis_Ababa** (so schedule times below
   are Addis time).
3. Clones this repo to `~/serago`.
4. Creates a virtualenv and installs `requirements.txt`.
5. Creates `~/serago/.env` from the template — **then stops** so you can paste
   your secrets (see Step 3). Re-running continues from here.
6. Checks the required DB vars are present; generates a real
   `DJANGO_SECRET_KEY` if the placeholder is still there.
7. Runs `migrate` + `seed_sources`.
8. Installs and enables the systemd **timer** (12:00 & 23:30 Addis).
9. Smoke-test: runs `scrape_all` once so you see results immediately.

Optional admin dashboard: `WITH_ADMIN=1 bash setup_vm.sh` — also serves
`/admin/` on port 8000 (gunicorn + whitenoise), collects static files, and
prints the public IP to add to `DJANGO_ALLOWED_HOSTS`.

### Step 3 — Secrets (the only manual part)

After the script creates `~/serago/.env`, open it and paste the same values
as your local `.env`:

```
DB_ENGINE=django.db.backends.postgresql
DB_NAME=...            # from your Neon connection string
DB_USER=...
DB_PASSWORD=...
DB_HOST=...
DB_PORT=5432
ETHIOJOBS_TOKEN=...    # same token as local .env (fresh one from ethiojobs.net/jobs Network tab if it expired)
JINA_API_KEY=...       # optional; free key from jina.ai raises the relay request limit
```

Then re-run `bash setup_vm.sh` — it picks up where it left off.

### Step 4 — Verify

```bash
systemctl list-timers serago-scrape.timer     # next fire time
journalctl -u serago-scrape -e                # last run logs
cd ~/serago && ./.venv/bin/python manage.py log_report   # day report
```

If you installed the admin dashboard: add the VM's public IP to
`DJANGO_ALLOWED_HOSTS` in `.env`, `sudo systemctl restart serago-admin`,
create a login with `./.venv/bin/python manage.py createsuperuser`, then open
`http://<public-ip>:8000/admin/`.

---

## 4. The schedule (today and later)

Everything lives in **one file**: `/etc/systemd/system/serago-scrape.timer`
(committed as `deploy/serago-scrape.timer`).

| When | Timer line | Result |
|---|---|---|
| Now | `OnCalendar=*-*-* 12:00,23:30` | Midday + 30 min before midnight, Addis time |
| Later | `OnCalendar=*-*-* *:00,30:00` | **Every 30 minutes** |

To switch:

```bash
sudo nano /etc/systemd/system/serago-scrape.timer
# change the OnCalendar line
sudo systemctl daemon-reload && sudo systemctl restart serago-scrape.timer
```

Why 30-minute runs stay cheap: every source filters to **today-only** and the
scraper stops as soon as it reaches a listing it already stored (`only_today`
+ incremental stop). A typical re-run fetches 1–2 pages per source — a few
minutes total — so load on the job sites and Neon stays low, and each day's
master log simply records more runs.

---

## 5. Day-to-day operations

- **Logs**: `journalctl -u serago-scrape -e` (each run's per-site summary is
  also stored in the DB — `ScrapeLog` + per-site logs, viewable in admin).
- **Report**: `manage.py log_report` — totals, per-website numbers, any
  non-200 API hits / failed runs.
- **Health check**: `manage.py daily_check` — tests + live structure diff vs
  the stored API snapshots + report.
- **Update the code**: on the VM, `bash ~/serago/deploy/update.sh`
  (pull + install + migrate + seed). The next timer tick uses the new code.
- **Missed runs**: `Persistent=true` fires a missed run right after boot if
  the VM was stopped — no data gaps.

---

## 5b. Get notified on Telegram (free, recommended)

After every scrape you get the `log_report` summary delivered to your phone
via a Telegram bot — no need to open the Actions tab. Works for both
deployment paths: the GitHub Actions workflow already calls
`manage.py telegram_report` after each run (`if: always()`, so it also
fires when a run fails — that's the point).

**One-time setup (~5 min, all in the Telegram app + GitHub):**

1. In Telegram, open **@BotFather** → `/newbot` → pick a name (e.g.
   `SeraGo Alerts`) → copy the **token** it gives you.
2. Message your new bot once (anything, e.g. `/start`), then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser — your
   numeric **chat id** is in the JSON under `message.chat.id`.
3. GitHub → repo → **Settings → Secrets and variables → Actions** → add
   two secrets:
   - `TELEGRAM_BOT_TOKEN` = the token from step 1
   - `TELEGRAM_CHAT_ID` = the chat id from step 2
4. Click **Run workflow** in the Actions tab — you should get the first
   report within a couple of minutes.

**How it notifies (two modes, configurable without touching code):**

| Mode (`NOTIFY_MODE`) | Behavior | When to use |
|---|---|---|
| `per-run` (default) | Report after **every** run (2/day now) | Today |
| `daily` | Only **failures** + one **end-of-day digest** on the final run | When you move to every-30-min (no spam) |

To switch later: repo → Settings → Secrets and variables → **Variables** →
set `NOTIFY_MODE=daily` (and `DAILY_DIGEST_UTC=20:30` if your last run's
UTC time differs — 20:30 UTC = 23:30 Addis). No code change, no push.

`DAILY_DIGEST_UTC` is the UTC time of the day's final scheduled run; the
command (`core/management/commands/telegram_report.py`) decides the digest
by comparing the current time to it, and flags a message with `⚠️` when the
day has failed runs or non-200 responses.

The digest message is the **full day report** — day status, run/website/api
totals, per-website found/inserted/skipped, and every failure — and on that
final run it also runs the Django test suite once and includes the result
(test count, pass/fail, duration), so the end-of-day message doubles as a
daily health check of the structure snapshots. Disable with
`manage.py telegram_report --skip-tests`.

---

## 6. Watch-items (all free-tier limits)

1. **Neon compute hours** — fine today; keep an eye on "Active time" if you
   go to every-30-minutes. If it ever gets tight, the Neon project's compute
   can be sized down to 0.25 CU (still free).
2. **Rate limits** — the GeezJobs source fetches through the free
   `r.jina.ai` relay: ~20 req/min without a key, ~500/min with a free
   `JINA_API_KEY`. At 30-minute cadence you're far under either; add the key
   anyway.
3. **EthioJobs token** — the JWT in `x-custom-header` expires; if EthioJobs
   starts failing, grab a fresh one and update `.env` (no code change).

---

## 7. When you start making money

Nothing about this setup breaks the day you monetize — it stays free forever.
If you later prefer managed hosting, the code needs **zero changes** to move:
it's plain Django + a management command + an external DB. The only thing
that changes is how the command is invoked (a cron line here, a Railway/Render
job there, or finally wiring up the Celery/Redis path in the overview doc).

---

## 8. Files in this repo that make this work

```
deploy/
  setup_vm.sh             one-shot VM setup (idempotent)
  update.sh               pull + migrate + seed on the VM
  serago-scrape.service   one scrape_all run (systemd oneshot)
  serago-scrape.timer     the schedule (12:00 & 23:30 Addis; */30 switch documented)
  serago-admin.service    optional Django admin dashboard (gunicorn)
DEPLOYMENT.md             this file
serago/settings.py        whitenoise middleware + STATIC_ROOT (for the admin)
requirements.txt          + whitenoise
```
