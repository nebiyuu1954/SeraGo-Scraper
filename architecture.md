# SeraGo — Architecture & "Add a New Website" Guide

SeraGo is a Django scraping service for Ethiopian job sites. It is built so that
**every website follows the exact same pattern**. Afriwork is the reference
implementation — this document is the playbook you follow for the next website
(and every website after it).

> **Golden rule:** every website gets **its own model for items** (e.g.
> `AfriworkJob`) and **its own model for logs** (e.g. `AfriworkScrapeLog`),
> and both are **related to the master models** (`ScrapedItem`, `ScrapeLog`)
> that aggregate all websites. Never push website-specific fields into the
> master models.

---

## 1. The two-level pattern (why it works)

```
MASTER LEVEL (shared, one row per day / one row per listing)
  ScrapedItem  — the universal listing contract (source, title, company, ...)
  ScrapeLog    — the master day log (day totals + compact per-website buckets)

PER-WEBSITE LEVEL (one model pair per website)
  AfriworkJob        — Afriwork-specific item details, linked from ScrapedItem
  AfriworkScrapeLog  — Afriwork-specific day log (full per-run detail)

  + Source row       — configuration only (endpoint, query, field mapping, ...)
  + scraper class    — GraphQLScraper (or a new scraper type)
  + structure snapshot file — core/structure_snapshots/<slug>.json
```

- **`ScrapedItem`** deduplicates on `(source, external_id)` and holds the
  normalized fields every website must provide (`title`, `company`, `location`,
  `job_type`, `url`, `published_at`, ...).
- **`ScrapeLog`** is ONE row per calendar day. It holds the day's totals
  (`api_hits`, `items_found/inserted/updated/skipped`, `run_count`,
  `websites_count`, worst `status`) plus one compact JSON (`websites`) with a
  short bucket per website: `{source, name, table, log_id, status, run_count,
  api_hits, items_*}`. The bucket's `table` + `log_id` point at that website's
  own log row — the master stays **lean**, all detail lives in the per-site log.
- **Per-website item model** mirrors the website's raw API payload (every field
  the API sends, plus a verbatim `raw_payload` JSON). It links back to the
  master item through a `OneToOneField` on `ScrapedItem` (e.g.
  `ScrapedItem.afriwork_job`).
- **Per-website log model** is ONE row per `(source, day)` whose `scraped_log`
  JSON grows with every run that day: each entry carries `status`, per-page
  `pages_hit` with `http_status`, `errors`, `message`, timings, `api_hits`.
  This is where you drill down when the master says a website failed.

---

## 2. Project layout

```
serago/settings.py                 Django settings (TIME_ZONE = Africa/Addis_Ababa)
core/
  models.py                        ALL models (master + per-website)
  admin.py                         ALL admin registrations
  tests.py                         The test suite
  structures.py                    structure-snapshot helpers
  reporting.py                     api_issues_for_day() — non-200 / failed-run detection
  structure_snapshots/<slug>.json  one structure snapshot per website
  scrapers/
    base.py                        BaseScraper pipeline (fetch→parse→normalize→save)
    graphql.py                     GraphQLScraper (Afriwork's scraper)
    rest.py                        RestJsonScraper (EthioJobs's scraper)
    factory.py                     ScraperFactory registry (scraper_type → class)
    __init__.py                    re-exports
  management/commands/
    seed_sources.py                create/update Source rows (idempotent)
    scrape_source.py               run one source now
    scrape_all.py                  run every active source (one command)
    capture_structure.py           snapshot a source's API structure
    check_structure.py             live-diff a source against its snapshot
    log_report.py                  day report (totals + any issues)
    daily_check.py                 one command: tests + structure + report
    clear_data.py                  wipe all data except Source (prod-safe)
COMMANDS.md                        command reference (all commands in one place)
```

---

## 3. Master models (`core/models.py`)

All models inherit `TimeStampedModel` (UUID pk + `created_at`/`updated_at`).

### `Source` — the configuration
Everything about *how* to scrape a website lives here, not in code:

| Field | Purpose |
|---|---|
| `name`, `slug` | display name + unique slug (used everywhere) |
| `scraper_type` | `graphql` / `rest` / `nextjs` / `html` / `playwright` |
| `endpoint` | the API URL to hit |
| `headers` | JSON dict of HTTP headers |
| `query` | the GraphQL query (or request params) |
| `field_mapping` | JSON: normalized field → dotted path (+ transforms) |
| `pagination` | JSON: paging rules (see §5) |
| `only_today` | when True, only listings dated today are fetched |
| `scrape_interval_hours`, `is_active` | scheduling / on-off |
| `last_scraped_at`, `last_success_at` | health timestamps (updated by the pipeline) |

### `ScrapedItem` — the universal listing
`source` FK, `external_id`, `title`, `description`, `company`, `location`,
`job_type`, `url`, `salary`, `content_hash` (SHA-256 dedup fingerprint),
`raw_data` (verbatim payload), `published_at`, `deadline`, `first_seen_at`,
`last_seen_at`, `is_active`, per-day `job_number` + `numbered_on`, and a
`OneToOneField` per website detail model.

Unique constraints: `(source, external_id)` and `(source, numbered_on,
job_number)`.

### `ScrapeLog` — the master day log (LEAN)
`day` (unique), `status` (worst across the day), `run_count`, `api_hits`,
`items_found/inserted/updated/skipped`, `websites` JSON (compact buckets, see
§1). Property `websites_count` = `len(websites)`.

**Do NOT add per-run or per-website detail here.** The detail belongs to the
per-website log.

---

## 4. Per-website models (the part you copy for each new site)

### 4.1 The item model — mirror the API
Copy the shape of AfriworkJob (or EthioJobsJob). Store **every field the website's API sends**,
plus `raw_payload` (the verbatim JSON) as a catch-all. It must include the same
per-day numbering fields so the per-site row mirrors the master item:

```python
class EthioJobsJob(TimeStampedModel):
    external_id = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    # ...one field per API key (string/JSON/datetime as appropriate)...
    raw_payload = models.JSONField(default=dict, blank=True)
    job_number = models.PositiveIntegerField(null=True, blank=True)
    numbered_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-numbered_on", "job_number"]

    @property
    def job_number_display(self) -> str:
        return f"{self.job_number:02d}" if self.job_number else "—"
```

Then add the reverse link on the master item (in `ScrapedItem`):

```python
ethiojobs_job = models.OneToOneField(
    "EthioJobsJob", on_delete=models.SET_NULL, null=True, blank=True,
    related_name="scraped_item_master",
)
```

### 4.2 The log model — one row per (source, day)
Copy the shape of `AfriworkScrapeLog` exactly:

```python
class EthioJobsScrapeLog(TimeStampedModel):
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="ethiojobs_day_logs")
    day = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=ScrapeStatus.choices, default=ScrapeStatus.SUCCESS)
    run_count = models.PositiveIntegerField(default=0)
    api_hits = models.PositiveIntegerField(default=0)
    items_found = models.PositiveIntegerField(default=0)
    items_inserted = models.PositiveIntegerField(default=0)
    items_updated = models.PositiveIntegerField(default=0)
    items_skipped = models.PositiveIntegerField(default=0)
    scraped_log = models.JSONField(default=list, blank=True)  # one entry per run

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["source", "day"], name="uniq_ethiojobs_daylog_src_day"),
        ]

    def last_run(self) -> dict | None:
        return self.scraped_log[-1] if self.scraped_log else None

    def __str__(self):
        return f"EthioJobs · {self.day} · {self.run_count} run(s) · {self.status}"
```

### 4.3 Register it in the master log's registry
At the bottom of `core/models.py` there is a registry — **append your new log
model** so the master rollup, `log_report`, and `api_issues_for_day` pick it up
automatically:

```python
SITE_LOG_MODELS = [AfriworkScrapeLog, EthioJobsScrapeLog]
```

That single line is what makes the website appear in the master log, the
report, and the health checks.

---

## 5. Source configuration (field_mapping & pagination JSON)

### `field_mapping` — dotted paths + transforms
Each key is a **normalized field name** (used by `ScrapedItem`); the value is a
dotted path into the raw item, optionally wrapped in a spec dict with
`transforms`:

```json
{
  "external_id": "id",
  "title": "title",
  "description": {"path": "description", "transforms": ["strip_html"]},
  "company": "entity.name",
  "location": {"path": "city.name", "transforms": ["clean_text"]},
  "job_type": {"path": "job_type", "transforms": ["upper"]},
  "published_at": {"path": "published_at", "transforms": ["parse_datetime"]},
  "deadline": {"path": "deadline", "transforms": ["parse_datetime"]}
}
```

Available transforms (extensible in `core/scrapers/base.py` → `TRANSFORMS`):
`strip_html`, `clean_text`, `upper`, `parse_datetime`, `job_type_code`
(numeric job-type code → shared JobType string, used by EthioJobs).

### `pagination` — how pages advance and how "today" is scoped
```json
{
  "page_size": 10,
  "results_path": "data.jobs",
  "limit_var": "limit",      // GraphQL variable name for page size (default limit)
  "offset_var": "offset",    // GraphQL variable name for offset (default offset)
  "date_filter": {"field": "published_at", "from_var": "from", "to_var": "to"},
  "max_pages": 50,
  "timeout": 30.0
}
```

- `page_size`, `results_path` are the essentials.
- `date_filter` + `Source.only_today` — **two modes**, chosen by the config:
  - **Server-side (GraphQL):** `{"field": "published_at", "from_var": "from",
    "to_var": "to"}` — the scraper injects today's local-day window into
    `$from`/`$to`. The **query** decides how the bounds apply — Afriwork uses
    `_or: [{published_at: {_gte: $from, _lt: $to}}, {refreshed_at: {_gte:
    $from, _lt: $to}}]` so REPOSTED jobs are captured too.
  - **Client-side (REST):** `{"field": "published_at"}` with **no**
    from/to vars — the API cannot filter by date, so the `RestJsonScraper`
    stops the sweep once a page contains no items from today (listings must
    arrive newest-first) and drops pre-today items on mixed pages.
  - REST pagination extras: `page_1_based: true` (EthioJobs numbers pages
    from 1), `page_key`/`limit_key` to rename query params, `params` for
    static query params.
  - If a website has no date field at all, set `only_today=False`.

---

## 6. Scraper layer

### BaseScraper pipeline (already written — you only subclass)
`fetch(page)` → `parse(raw)` → `normalize(raw_item)` → `save_items()`.
Everything config-driven. The base also provides:

- Per-day numbering: `_next_job_number()`, `_insert_item()` — items are sorted
  chronologically so **#01 = the first job posted that day**; later scrapes
  append new jobs with the next numbers.
- Incremental stop: `_today_known_ids()` + `_incremental_stop_safe()` — on
  re-scrapes the sweep stops as soon as it reaches a record already stored for
  today, so only newly posted jobs are fetched. The safety check reads the
  per-site day log's worst status (a failed/partial run today ⇒ full sweep).
- Client-side today hooks (used by `RestJsonScraper`): `_keep_item(item)`
  drops pre-today items on mixed pages; `_past_today_boundary(page, items)`
  ends the sweep once a page contains no items from today (defaults are
  no-ops — server-side filtered scrapers don't need them).
- **Generic day-log writing:** `record_detail_log()` is implemented ONCE in
  the base class — a concrete scraper only sets a class attribute:
  ```python
  class EthioJobsScraper(RestJsonScraper):
      site_log_model = EthioJobsScrapeLog
  ```
  The base appends `_run_summary(run)` to the site log, bumps totals + worst
  status, and returns the pk — no per-site copy/paste needed.
- Run orchestration: `scrape()` (one page) and `scrape_many()` (sweep), both
  finalize via `_close()` which writes the per-site log first, then the master
  day log. Every HTTP request is counted in `api_hits`/`pages_hit`.
- `last_run()` — the most recent run entry, read from the per-site log.

### Concrete scraper types
- **`GraphQLScraper`** (Afriwork) — POST a query with variables; `date_filter`
  with from/to vars ⇒ server-side today window. `site_log_model =
  AfriworkScrapeLog`.
- **`HaHuJobsScraper`** (HaHu Jobs, second GraphQL site) — subclasses
  `GraphQLScraper` so the whole pipeline is reused; it only adds
  `site_log_model = HaHuScrapeLog`, the `HaHuJob` detail row, a location
  derived from the nested `job_cities` list, and a `save_items()` override
  that drops ethiojobs-sourced listings (we scrape EthioJobs directly).
  Because it shares `scraper_type=graphql` with Afriwork, the factory
  dispatches it by **slug**: `ScraperFactory._slug_registry` maps
  `"hahujobs"` → `HaHuJobsScraper`, checked before `scraper_type`. A future
  second REST site registers its scraper class the same way.
- **`RestJsonScraper`** (EthioJobs) — GET a paged JSON API (`results_path`,
  `page_1_based`, `page_key`/`limit_key`); client-side today filter via
  `_keep_item`/`_past_today_boundary`. Auth tokens read from
  `settings.ETHIOJOBS_TOKEN` and injected into the `x-custom-header` (a
  longer-lived token goes in `.env`; the seed stores an empty placeholder).
- **`HtmlScraper` + `GeezJobsScraper`** (GeezJobs, server-side HTML) — no JSON
  API: the listings are baked into `.opportunity-card` divs on /search-jobs.
  `HtmlScraper` is the generic GET/HTML base (page 1 = bare URL, `?page=N`
  from page 2 — or `page_style: "path"` for WordPress `/page/N/` URLs;
  client-side today filter). `GeezJobsScraper` adds the card parsing,
  estimates `published_at` from the relative "Posted: X ago" chip, splits
  employment into time + type, and treats a page with no cards AND no search
  UI as a bot-check page (the site embeds a `.trap-field` honeypot that is
  never submitted — GETs only) by raising instead of silently storing
  nothing. Registered per-slug in `ScraperFactory` like HaHuJobs.
- **`ReporterJobsScraper`** (Ethiopian Reporter Jobs, second HTML site —
  WordPress / Noo Job Board theme) — subclasses `HtmlScraper`; path-style
  `/page/N/` pagination via `page_style: "path"`. The `article.noo_job`
  cards carry EXACT timestamps (`<time datetime="...">`), so `published_at`
  needs no estimation, and the closing-date span is the deadline. The
  newspaper posts in batches, so the strict today-only run truthfully stores
  nothing on non-posting days; a pinned "featured" widget repeats the same
  jobs on every page (dedup by WordPress post id). Behind Cloudflare — a
  challenge page (no cards, no `div.jobs.posts-loop` archive) raises
  `ScrapeError` instead of recording an empty feed. Registered per-slug in
  `ScraperFactory` (`"reporterjobs"` → `ReporterJobsScraper`, detail rows
  `ReporterJob` + `ReporterScrapeLog`).

Note on client-side filters: `_keep_item`/`_past_today_boundary` are designed
for DATE-based filtering (an all-old page legitimately ends the sweep).
Source-based filters (e.g. HaHuJobs skipping ethiojobs listings) must NOT use
`_keep_item` — an all-ethiojobs page would look empty and truncate the sweep.
Filter inside `save_items()` instead, and count the dropped items as
`skipped`, so pagination and the incremental-stop boundary stay correct.

### What a NEW concrete scraper must implement/override
```python
class EthioJobsScraper(BaseScraper):       # or reuse RestJsonScraper/GraphQLScraper
    site_log_model = EthioJobsScrapeLog    # the only log-related line

    def fetch(self, page=0): ...           # GET/POST one page; call _record_api_call()
    def parse(self, raw): ...              # return list of raw item dicts
    def _save_detail(self, item, instance): ...   # create/update the site's Job model, link it
    # optional: _keep_item / _past_today_boundary for client-side today filter
```

### Register a scraper type (only needed for a brand-new kind of site)
`core/scrapers/factory.py`:
```python
ScraperFactory.register(ScraperType.HTML, HtmlScraper)   # or NEXTJS / PLAYWRIGHT
```
GraphQL sites reuse `GraphQLScraper`; paged GET/JSON sites reuse
`RestJsonScraper` — no factory change.

### Seed the Source row
Append to `core/management/commands/seed_sources.py` (idempotent
`update_or_create(slug=...)` with the query, headers, field_mapping and
pagination), then run `python manage.py seed_sources`.

---

## 7. Structure snapshot — change detection per website

Each website gets a **snapshot file** recording the flattened dotted-path
fields of one listing item:

```
python manage.py capture_structure ethiojobs      # writes core/structure_snapshots/ethiojobs.json
python manage.py check_structure [slug]           # live-diff vs snapshot (exit != 0 on change)
```

The snapshot is version-controlled, so a website silently changing its API
shows up as a git diff AND as a failing `daily_check`. The test suite guards
that the snapshot still contains the core fields the scraper relies on.

---

## 8. Management commands (your daily workflow)

| Command | What it does |
|---|---|
| `seed_sources` | create/update all Source rows |
| `scrape_source <slug>` | run one source now (`--page N`, `--no-today`) |
| `scrape_all` | run EVERY active source with one command (see `COMMANDS.md`) |
| `log_report [--day D] [--all]` | day totals + per-website numbers + any non-200 / failed runs |
| `check_structure [slug]` | live structure diff vs snapshot |
| `capture_structure <slug>` | (re)write the structure snapshot |
| `daily_check [--day D] [--skip-tests]` | **the one command**: tests + live structure check + log report |
| `clear_data [--force] [--yes]` | wipe all data except Source (dev-guarded) |

---

## 9. Admin (`core/admin.py`)

Register the new models following the existing pattern:

- `EthioJobsJobAdmin` — like `AfriworkJobAdmin` (list_display with
  `job_number_display`, title, entity/company, location, job_type, published
  date; filters; readonly numbering fields).
- `EthioJobsScrapeLogAdmin` — like `AfriworkScrapeLogAdmin` (day, source,
  status_colored, totals, `scraped_log_pretty`).
- Add the new `OneToOneField` to `ScrapedItemAdmin.readonly_fields` if you want
  it visible there.

---

## 10. Tests (`core/tests.py`)

Add the new website's sample payload to the test suite (following
`AFRIWORK_SAMPLE`) and extend:

- `StructureTests` — a sample whose flattened paths must stay a subset of the
  new snapshot (`test_<site>_snapshot_contains_core_structure`).
- `ApiIssueTests` — site log with a non-200 page must be reported by
  `api_issues_for_day` and shown by `log_report`.
- `DayLogTests` — master bucket references the new site log (`table`/`log_id`),
  accumulation across runs, incremental-stop safety.

---

## 11. THE NEW-WEBSITE CHECKLIST (follow in order)

1. **Inspect the website** (Network tab — see §12 for exactly what to grab).
2. **Decide the dedup key carefully.** `external_id` must be the STABLE
   identifier. EthioJobs' encrypted `id` rotates on every request (verified
   live!) — its `slug` is stable, so that's the key; the rotating id is kept
   in a separate `api_id` column for reference. If in doubt, fetch the same
   listing twice and compare.
3. **Add the per-website item model** (`EthioJobsJob`) mirroring the API payload
   + `job_number`/`numbered_on`; add the `OneToOneField` on `ScrapedItem`.
4. **Add the per-website log model** (`EthioJobsScrapeLog`) — copy
   `AfriworkScrapeLog`.
5. **Append it to `SITE_LOG_MODELS`** in `core/models.py`.
6. **Write the scraper** — reuse `RestJsonScraper` (GET/JSON) or
   `GraphQLScraper` (query POST), or write a new type implementing
   `fetch`/`parse`/`_save_detail`; set `site_log_model`; add
   `_keep_item`/`_past_today_boundary` only if today is filtered client-side.
7. **Add the Source row** to `seed_sources.py` (query, headers, field_mapping,
   pagination, only_today/date_filter) and run `python manage.py seed_sources`.
8. **Migrate**: `makemigrations core && migrate`.
9. **Scrape**: `python manage.py scrape_source <slug>` — verify #01 ordering,
   dedup, today-only boundary, and that a second scrape only adds new jobs
   (should fetch 1 page and skip everything).
10. **Snapshot**: `python manage.py capture_structure <slug>`; commit the file.
11. **Register admin** for the two new models.
12. **Extend tests** with the sample payload + the three test families.
13. **Verify**: `python manage.py daily_check` → all three sections pass.

---

## 12. What to capture from the Network tab for a new website

When inspecting `https://ethiojobs.net/jobs` (DevTools → Network, reload,
filter **Fetch/XHR**), grab:

1. **The API endpoint(s)** — the exact URL(s) the page calls to load jobs
   (right-click → Copy as cURL).
2. **Request method + headers** — GET vs POST; any auth/token/cookie headers.
   If POST, **Copy as cURL includes the request body** — keep it verbatim.
3. **One full job object** — paste the complete JSON of a single job from the
   response (all keys, nested objects, nulls). This drives the item model.
4. **Pagination** — how page/limit/offset are sent (query params or body
   fields); what the total count looks like; what an empty last page returns.
5. **Ordering & date filtering** — is the list sorted by posted date? Is there
   a `posted_at`/`date` field per job? Can the request filter by date range
   (query param or body field)? This decides `date_filter`/`only_today`.
6. **Job detail URL pattern** — the link format for one job (e.g.
   `https://ethiojobs.net/job/1234`) for the item `url` field.
7. **Any required cookies / CSRF tokens / JWT headers** — whether anonymous
   requests work or the scraper must send extra headers (EthioJobs requires a
   JWT in `x-custom-header`; tokens go in `.env` as `ETHIOJOBS_TOKEN`).
8. **Dedup key stability** — fetch the same page twice; if the id changes
   between responses (EthioJobs does this), find a stable key (slug, URL,
   title+date) before writing any code.

With those eight things, the checklist in §11 is mechanical.
