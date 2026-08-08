# SeraGo — Command Reference

Every command is a Django management command, run through `manage.py`. On this
project, use the virtualenv's Python:

```
.venv/Scripts/python.exe manage.py <command> [options]
```

(On Linux/macOS that's `./venv/bin/python manage.py ...` — or activate the
virtualenv and just run `python manage.py ...`.)

---

## Quick reference

| Command | What it does |
|---|---|
| `seed_sources` | Create/update the built-in source configs (idempotent) |
| `scrape_source <slug>` | Scrape ONE source (sweep today's listings) |
| `scrape_all` | Scrape EVERY active source with one command |
| `capture_structure <slug>` | Snapshot a source's API structure for change detection |
| `check_structure [slug]` | Diff a source's live structure against its snapshot |
| `log_report [--day] [--all]` | Day report: totals, per-website numbers, API issues |
| `daily_check [--day] [--skip-tests]` | The one-command health check (tests + structure + report) |
| `clear_data [--yes] [--force]` | Wipe all scraped data, keep the Source config |

---

## Typical workflow

```bash
# 1. Apply migrations (first time / after an update)
.venv/Scripts/python.exe manage.py migrate

# 2. Create/refresh the source configurations
.venv/Scripts/python.exe manage.py seed_sources

# 3. Scrape everything
.venv/Scripts/python.exe manage.py scrape_all

# 4. See how the day went
.venv/Scripts/python.exe manage.py log_report

# 5. Daily health check (tests + structure diff + report)
.venv/Scripts/python.exe manage.py daily_check
```

---

## Commands in detail

### `seed_sources`
Create or update the built-in `Source` configurations (Afriwork, EthioJobs,
HaHuJobs, GeezJobs, Ethiopian Reporter Jobs). Idempotent — safe to run any
time; run it after pulling an update that adds or changes a source.

```
.venv/Scripts/python.exe manage.py seed_sources
```

---

### `scrape_source <slug>`
Scrape one source. Default: sweep pages until today's listings are covered
(today-only filter applies). The run's stats land in the source's own
per-site day log; the master `ScrapeLog` keeps the day's overall totals.

```
.venv/Scripts/python.exe manage.py scrape_source afriwork
.venv/Scripts/python.exe manage.py scrape_source ethiojobs --no-today   # ignore today-only
.venv/Scripts/python.exe manage.py scrape_source hahujobs --page 2      # single page (debug)
```

Options:
- `--no-today` — disable the source's today-only filter for this run.
- `--page N` — scrape only the single 0-based page N (debug).

---

### `scrape_all`
Scrape every **active** source with one command. It calls `scrape_source`
for each source in order, so every website logs exactly as if you had run
them individually — same per-site day logs, same master log. If one source
fails, the others still run; the exit code is non-zero if anything failed
(handy for cron).

```
.venv/Scripts/python.exe manage.py scrape_all
.venv/Scripts/python.exe manage.py scrape_all --no-today
.venv/Scripts/python.exe manage.py scrape_all --slugs afriwork ethiojobs
.venv/Scripts/python.exe manage.py scrape_all --page 2
```

Options:
- `--no-today` — passed to every source's scrape.
- `--slugs a b c` — scrape only these sources (default: all active).
- `--page N` — passed to every source (single-page debug run).

---

### Fetch relays (why some sources don't hit the site directly)

A source can fetch through a relay so the target site never sees your IP.
`HtmlScraper` supports `pagination.relay: "jina"` (e.g. GeezJobs): the page
URL is percent-encoded into `https://r.jina.ai/<url>` and fetched from Jina's
infrastructure with `X-Return-Format: html` (raw HTML, so parsing is
unchanged) and `X-No-Cache: true`. GeezJobs' Hostinger WAF blocks our network
(403 on every path) but does not block Jina's, so the relay keeps that source
scraping. Note: the logged `http_status` is the relay's response, not the
target site's (a relayed 403 still shows as 200 in `pages_hit`; a blocked
target surfaces as a `ScrapeError` from parsing). Optional: set `JINA_API_KEY`
in the environment (or `.env`) for the
free tier's higher request limits. To fetch a source directly again, remove
`"relay": "jina"` from its pagination config in `seed_sources.py` and re-run
`seed_sources`.

---

### `capture_structure <slug>`
Fetch one page from a source's live API and write the flattened field paths
of the first listing to `core/structure_snapshots/<slug>.json`. Run this
once per new source (or after the site changes and you accept the new shape).

```
.venv/Scripts/python.exe manage.py capture_structure geezjobs
```

---

### `check_structure [slug]`
Compare a source's live structure against its stored snapshot. Any added or
removed field is reported and the command exits non-zero — so a silent API
change becomes visible immediately.

```
.venv/Scripts/python.exe manage.py check_structure          # every active source
.venv/Scripts/python.exe manage.py check_structure afriwork # one source
```

---

### `log_report [--day YYYY-MM-DD] [--all]`
Print a report of the day's scrape logs: master totals, one line per
website, then any issues — page hits that did not return HTTP 200 (which
website/page/log row) and failed or partial runs (with their errors).

```
.venv/Scripts/python.exe manage.py log_report               # today
.venv/Scripts/python.exe manage.py log_report --day 2026-08-07
.venv/Scripts/python.exe manage.py log_report --all         # every day present
```

---

### `daily_check [--day YYYY-MM-DD] [--skip-tests]`
The one-command health check. Runs in order:
1. The test suite (structure snapshots, API-status detection, log rollups).
2. Live structure comparison for every active source (`check_structure`).
3. Today's log report (`log_report`).

Exit code is non-zero if any test fails or any structure changed.

```
.venv/Scripts/python.exe manage.py daily_check
.venv/Scripts/python.exe manage.py daily_check --skip-tests
```

---

### `clear_data [--yes] [--force]`
Delete ALL scraped data — `ScrapedItem`, `ScrapeLog`, per-site detail rows
and day rollups — but keep the `Source` configuration so you can re-scrape
immediately. Intended for production resets.

```
.venv/Scripts/python.exe manage.py clear_data --yes
```

Safety rails:
- Refuses to run when `DEBUG=True` (dev DB) unless `--force` is given.
- Always asks for confirmation unless `--yes` is given.

---

## Not a command: what `manage.py shell` is for

For ad-hoc inspection (e.g. counting rows, poking at a scraper), use the
Django shell:

```
.venv/Scripts/python.exe manage.py shell
```
