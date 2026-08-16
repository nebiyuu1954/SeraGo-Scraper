# Job Lifecycle — from scraped to archived

This is the full journey of a job listing, from the moment the scraper
discovers it on a source website until it is archived to a file and removed
from the database. Two projects are involved: **SeraGo-Scraper** (Django —
collects + stores raw listings, keeps persistent stats) and **SeraGo**
(.NET backend + React frontend — imports listings into the product, shows
them to users).

```
 GITHUB ACTIONS (scheduled)                      SERAGO BACKEND (Render)
┌──────────────────────────────┐                 ┌──────────────────────────────┐
│ scrape_all (5 sources)       │                 │ SyncScheduler / admin POST   │
│   fetch pages ──► parse ──►  │                 │ /api/admin/sync/scraped-jobs │
│   normalize ──► dedupe ──►   │    incremental  │                               │
│   upsert ScrapedItem +       │ ────read───►    │ import + normalize sectors   │
│   per-site detail row        │    (watermark)  │ into SeraGo Jobs table       │
│   (today filter: only_today) │                 │                               │
└──────────────┬───────────────┘                 └──────────────┬────────────────┘
               │                                                   │
               ▼                                                   ▼
    day logs (ScrapeLog +          persistent stats            public feed
    per-site logs)                 ScrapeStat (day/week/       shows job while
        │                          month/year) + CategoryStat  deadline > now − 7
        │                          (sectors) — NEVER deleted   (deadline + 7 grace)
        ▼                                                   │
    Telegram report (daily issues, weekly/monthly stats)    │
        │                                                   │
        ▼                                                   ▼
 SUNDAY 23:45 Addis — archive_week --step sunday        JOB LIFE ENDS:
   files every past-window job (deadline + 7) +         deadline passes → 7-day
   all logs → jobs-YYYY-MM-DD.jsonl.gz +                grace → removed from feed;
   logs-YYYY-MM-DD.json.gz → Telegram →                 saved-job users see a timer,
   delete jobs + Mon–Sat logs, KEEP Sunday,             then it leaves saved lists
   write ArchiveRun sent-note
        │
        ▼
 MONDAY 18:00 Addis — archive_cleanup --step monday
   note exists → delete Sunday's rows (new week starts)
   note missing → RETRY the archive right now (files +
   sends + deletes); failure → ⚠️ Telegram warning, you
   fix + rerun manually. No nightly retries.
```

## Stage by stage

1. **Scrape** — a scheduled GitHub Actions run calls `manage.py scrape_all`.
   Each source has its own scraper (Afriwork = GraphQL, EthioJobs = REST,
   HaHu = GraphQL, GeezJobs + ReporterJobs = HTML via the Jina relay). Pages
   are fetched with retries (transport errors, plus extra relay retries for
   transient 403/429/5xx), parsed, and only **today's** listings are kept
   (`only_today`) — the sweep stops at the first page with nothing from
   today, so a quiet run costs a few pages, not the whole catalog.

2. **Normalize + store** — each listing becomes one `ScrapedItem` (master,
   deduped by `(source, external_id)`) plus a per-site detail row
   (`AfriworkJob`, `EthioJobsJob`, `HaHuJob`, `GeezJob`, `ReporterJob`) that
   keeps the site-specific fields and the raw payload. Missing deadlines get
   `published + 30 days` and are flagged (`deadline_is_default`) so the daily
   report nags until the source mapping is fixed.

3. **Log + stats** — every run appends to the day logs (`ScrapeLog` master +
   per-site log rows) and recomputes the **persistent** stats: `ScrapeStat`
   day/week/month/year rollups and `CategoryStat` day-granular sector counts.
   These are never deleted — they are the permanent record after the raw
   logs are archived.

4. **Report** — the Telegram report fires after runs (per-run mode, or
   failure-only + end-of-day digest in daily mode): day totals, non-200
   responses, failed/partial runs, sources that logged success with 0 items
   on a non-Sunday (possible silent failure), defaulted deadlines, and the
   weekly (Sunday) / monthly (month-end) stat sections.

5. **Sync to SeraGo** — the SeraGo backend imports active listings from the
   scraper DB (raw SQL over the same Neon database). The first run is a full
   bootstrap; afterwards it is incremental (`WHERE updated_at > watermark`),
   so each sync costs O(what changed). Sectors are normalized into the
   canonical vocabulary (unknown sector names are reported), jobs get a
   `(source, external_id)` key, and vanished listings are deactivated.

6. **Life on the site** — a job is visible while `deadline > now − 7 days`
   (the shared `LIFECYCLE_GRACE_DAYS` / `JobLifecycle.GraceDays` contract).
   Saved jobs survive the cleanup via a snapshot link; when the grace period
   ends, the job leaves the feed and saved lists (a timer is shown before
   that). Sync ignores past-window rows, so deleted jobs are never resurrected.

7. **Archive (Sunday 23:45 Addis)** — `archive_week --step sunday` files
   everything still in the DB: every job past `deadline + 7` and all log
   rows, into `jobs-YYYY-MM-DD.jsonl.gz` + `logs-YYYY-MM-DD.json.gz`, sent
   to Telegram. On success it deletes the jobs and the **Mon–Sat** logs,
   keeps **Sunday's** rows (so you can inspect Sunday on Monday), and writes
   an `ArchiveRun` "sent" note. On failure it sends a ⚠️ warning and deletes
   nothing — Monday retries.

8. **Cleanup (Monday 18:00 Addis)** — `archive_week --step monday`: with a
   sent-note it deletes Sunday's kept rows (the new week starts fresh); with
   no note it **retries the archive right now**. If that also fails, ⚠️
   Telegram warning, nothing deleted, and you fix + rerun via the workflow's
   "Run workflow" button.

## What is never deleted

- `ScrapeStat` (day/week/month/year rollups) and `CategoryStat` (sector
  counts) — the basis of the admin stats API
  `GET /api/admin/stats/top?period=day|week|month|year` on the SeraGo
  backend, which the frontend dashboard consumes.
- The archived `.gz` files in Telegram (the only copy of the raw payloads
  and per-run logs once the DB rows are deleted).

## Failure modes covered

- Relay/site 403 → retried, then the run fails loudly (never a silent empty
  success).
- JS-required skeleton page (ReporterJobs) → raises `ScrapeError` instead of
  logging success with 0 items.
- Silent zero (success + 0 found on a non-Sunday) → flagged in the report.
- Archive send failure → nothing deleted, retried Monday, you get warned.
- Grace-window mismatch → one constant both projects share; sync ignores
  past-window rows; cleanup deletes only past-window rows.
