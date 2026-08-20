# Cloudflare Bypass — SeraGo Anti-Bot Strategy

This document captures everything we know about bypassing Cloudflare-protected
job sites. When a new site triggers a Cloudflare challenge, consult this file
first — it has the exact symptoms, the rotation logic, the credit math, and
the fallback chain.

---

## 1. How we detect Cloudflare

Cloudflare's bot-check interstitial ("Just a moment…") is returned as HTTP 200
by most relay services — the relay itself gets through, but the BODY is the
challenge page, not the real content. The scraper detects it by body content:

```python
CHALLENGE_MARKERS = (
    "just a moment",
    "challenges.cloudflare.com",
)
```

When ALL of these markers are absent from a real page, the page is genuine.
When the body is a challenge page, `is_cloudflare_challenge()` returns True
and the scraper retries with backoff, then fails with a clear message.

**Important:** `/cdn-cgi/challenge-platform` scripts appear on REAL
Cloudflare-fronted pages too — it is NOT a challenge marker.

---

## 2. The rotation backends

We maintain 6 backends (1 reader relay + 5 anti-bot APIs), each with a
free tier. The scraper rotates across them cheapest-first.

| # | Service | Type | API URL | Auth | Request format | Response format | Free tier/mo | Credits/req |
|---|---------|------|---------|------|----------------|-----------------|-------------|-------------|
| 1 | **Jina Reader** | Relay | `https://r.jina.ai/<url>` | Bearer header | GET path-based (`r.jina.ai/ENCODED_URL`) | HTML body | **6,000** (200/day) | 1 |
| 2 | **Scrape.do** | API | `https://api.scrape.do/` | `token` param | GET `?token=KEY&url=URL&render=true` | HTML body | 1,000 | 1 |
| 3 | **ScrapeBadger** | API | `https://scrapebadger.com/v1/web/scrape` | `x-api-key` header | POST JSON `{"url": "URL", "format": "html"}` | JSON `{"content": "HTML"}` | 1,000 | 1-3 |
| 4 | **ZenRows** | API | `https://api.zenrows.com/v1/` | `apikey` param | GET `?apikey=KEY&url=URL&js_render=true&premium_proxy=true` | HTML body | 5,000 | 25 |
| 5 | **ScraperAPI** | API | `https://api.scraperapi.com` | `api_key` param | GET `?api_key=KEY&render=true&url=URL` | HTML body | 1,000 | 5-75 |
| 6 | **ScrapFly** | API | `https://api.scrapfly.io/scrape` | `key` param | GET `?url=URL&key=KEY&asp=true&render_js=true` | JSON `{"result": {"content": "HTML"}}` | 1,000 | 30-80 |

### Key differences

- **Jina Reader**: Free relay that fetches any URL and returns raw HTML.
  Uses path-based URL encoding (`r.jina.ai/<encoded-target-url>`). Cheapest
  option — bypasses WAFs by fetching from Jina's own infra. Best for
  sources like GeezJobs where the site blocks our IP but not Jina's.
- **Scrape.do**: 1 credit per request regardless of features. Best value
  among API backends. `render=true` for JS.
- **ScrapeBadger**: POST endpoint (unique). Returns JSON envelope.
- **ZenRows**: Premium proxies + JS rendering = 25 credits/request.
  Best free tier among API backends (5,000 credits).
- **ScraperAPI**: Simplest API. Credit cost varies (5-75).
- **ScrapFly**: Most expensive. `asp=true` for anti-bot.

### Key differences

- **ZenRows**: Premium proxies + JS rendering = 25 credits/request. Best
  free tier (5,000 credits). `mode=auto` escalates automatically.
- **Scrape.do**: 1 credit per request regardless of features. Best value.
  `render=true` for JS, `super=true` for residential proxies.
- **ScrapeBadger**: POST endpoint (unique among these). Returns JSON envelope
  with `content` field. `format=html` for raw HTML.
- **ScrapFly**: Already integrated. `asp=true` for anti-bot, `render_js=true`
  for JS. Returns JSON envelope with `result.content` + `result.status_code`.
- **ScraperAPI**: Simplest API. `render=true` for JS. Returns raw HTML.
  Credit cost varies by feature (5 for basic, 75 for JS+premium proxy).

---

## 3. Smart rotation logic

The rotation is cheapest-first with monthly credit awareness:

```
For each Cloudflare request:
  1. Check which services have API keys configured
  2. Check which services still have credits this month (tracked in DB)
  3. Pick the service with the most remaining credits
     (ties broken by: cheapest first → Scrape.do > ScrapeBadger > ZenRows > ScraperAPI > ScrapFly)
  4. If all services are exhausted, SKIP this source (don't burn retries)
  5. Log which service was used for auditing
```

### Rotation modes

There are **two rotation modes**, depending on the source's needs:

**`relay_rotate`** — for reader-relay sources (e.g. GeezJobs):
```
Jina → Scrape.do → ScrapeBadger → ZenRows → ScraperAPI → ScrapFly
```
Tries Jina first (cheapest, free). If Jina fails (402 quota, 403, 5xx),
falls back to CF backends in cheapest-first order.

**`cloudflare_rotate`** — for CF-protected sites (e.g. ReporterJobs):
```
Scrape.do → ScrapeBadger → ZenRows → ScraperAPI → ScrapFly
```
Same as above but without Jina (CF backends only).

**Why Jina first in relay_rotate:**
1. Jina: 1 credit/req, 6,000 free/mo — cheapest by far
2. Scrape.do: 1 credit/req, 1,000 free — best CF backend
3. ScrapeBadger: 1-3 credits/req, 1,000 free
4. ZenRows: 25 credits/req, 5,000 free = 200 requests
5. ScraperAPI: 5-75 credits/req, 1,000 free
6. ScrapFly: 30-80 credits/req — most expensive

### Credit tracking

Each successful request is logged in `ScraperCreditUsage`:
- `service`: which backend was used
- `credits_used`: estimated cost (1 for Scrape.do, 25 for ZenRows, etc.)
- `month`: YYYY-MM for monthly reset
- `source_slug`: which site triggered the request

The scraper checks remaining credits before each request. When a service
hits 0, it's skipped for the rest of the month.

---

## 4. Budget math

### Combined free budget (all 6 backends)

| Service | Credits/mo | Credits/req | Requests/mo |
|---------|-----------|-------------|-------------|
| Jina Reader | 6,000 | 1 | **6,000** |
| Scrape.do | 1,000 | 1 | 1,000 |
| ScrapeBadger | 1,000 | 1-3 | 333-1,000 |
| ZenRows | 5,000 | 25 | 200 |
| ScraperAPI | 1,000 | 5-75 | 13-200 |
| ScrapFly | 1,000 | 30-80 | 12-33 |
| **TOTAL** | | | **~7,558-8,433** |

### Per-site projections (current: 2 runs/day)

| Sites | Requests/mo each | Total/mo | Budget used | Status |
|-------|-----------------|----------|-------------|--------|
| 1 | 60 | 60 | 2-4% | ✅ Trivial |
| 2 | 60 | 120 | 5-8% | ✅ Easy |
| 3 | 60 | 180 | 7-12% | ✅ Comfortable |
| 5 | 60 | 300 | 12-19% | ✅ Fine |
| 10 | 60 | 600 | 25-39% | ✅ Works |
| 15 | 60 | 900 | 37-58% | ⚠️ Watch budget |

### If you increase to 4x/hour (peak) + 1x/hour (off-peak)

Peak (18h): 72 runs/day × N sites
Off-peak (6h): 6 runs/day × N sites
Total: 78 runs/day × N sites × 30 days

| Sites | Requests/mo | Budget used | Status |
|-------|-------------|-------------|--------|
| 1 | 2,340 | 96-150% | ⚠️ Tight — needs 2+ services |
| 2 | 4,680 | 192-300% | ❌ Needs paid tier |
| 5 | 11,700 | 480-750% | ❌ Needs paid plan ($19-49/mo) |

**Bottom line:** 2 runs/day is comfortable for 15+ sites on free tiers.
Every-30-min needs 1-2 paid services ($19-29/mo) for 5+ Cloudflare sites.

---

## 5. Current protected sites

| Site | Slug | Protection | Relay mode | Last verified | Backend used |
|------|------|-----------|------------|---------------|--------------|
| GeezJobs | `geezjobs` | Hostinger WAF (403) | `relay_rotate` | 2026-08-18 | Jina primary, CF fallback |
| Ethiopian Reporter Jobs | `reporterjobs` | Cloudflare (Turnstile) | `cloudflare_rotate` | 2026-08-18 | Rotating (all 5 CF backends) |

### ReporterJobs specifics

- **Theme**: Careerfy (WordPress, switched mid-2026 from Noo Job Board)
- **Cards**: `div.jobsearch-joblisting-classic-wrap`
- **Pagination**: AJAX "Load more" — max_pages=1 only
- **Timestamps**: Relative only ("Published X hours ago") — estimated
- **Deadlines**: Theme has deadline field disabled — +30 day default
- **JS rendering required**: Yes (Careerfy loads cards via JS)

---

## 6. Adding a new site

### Adding a WAF-blocked site (like GeezJobs)

If the site blocks our IP but not external relays:

1. Set `pagination.relay = "relay_rotate"` in `seed_sources.py`
2. Set Jina as the primary relay — it's cheapest and most sites don't
   block Jina's infrastructure
3. The CF backends act as automatic fallback if Jina fails

### Adding a Cloudflare-protected site (like ReporterJobs)

1. Set `pagination.relay = "cloudflare_rotate"` in `seed_sources.py`
2. The scraper auto-rotates across all configured backends
3. No code change needed — the rotation picks the best available backend

### Adding a new backend

To add a new anti-bot service to the rotation:

1. Subclass `CloudflareBackend` in `core/cloudflare_backends.py`
2. Set class attributes (name, api_url, env_key, etc.)
3. Implement `build_request_kwargs` and `parse_response`
4. Add to `DEFAULT_ROTATION_ORDER` or `RELAY_ROTATION_ORDER`
5. Add the env var to `.env.example`

That's it — the rotation, credit tracking, and admin dashboard all
pick it up automatically.

---

## 7. If all backends fail

Symptoms: every service returns the challenge page or 403.

**Immediate**: the run fails with "Blocked by Cloudflare challenge even
through anti-bot proxy on all N attempt(s)" — check which services were
tried in the logs.

**Root causes**:
1. **Site is in Under Attack mode** — even premium proxies get challenged.
   Wait 1-24 hours; these modes are temporary.
2. **All free tiers exhausted** — check `ScraperCreditUsage` in admin.
   The next month resets automatically.
3. **API keys expired/invalid** — the error names the specific HTTP status
   (401/402/403) and which service rejected the request.

**Recovery options**:
- Wait for Under Attack mode to lift (usually 1-24h)
- Upgrade the cheapest service (ZenRows $19/mo → 45,000 credits)
- Use a residential proxy service directly (Bright Data, Oxylabs — $10-50/mo)

---

## 8. Credits dashboard query

To check current month's usage across all services:

```sql
SELECT service, SUM(credits_used) as total_credits, COUNT(*) as requests
FROM core_scrapercreditusage
WHERE month = '2026-08'  -- adjust to current YYYY-MM
GROUP BY service
ORDER BY total_credits DESC;
```

To check which service was used for each request:

```sql
SELECT service, source_slug, created_at
FROM core_scrapercreditusage
WHERE month = '2026-08'
ORDER BY created_at DESC
LIMIT 50;
```

---

## 9. Environment variables

All keys are optional — the scraper uses whichever are configured.
The rotation skips backends whose key is missing.

```
# Primary relay (for relay_rotate sources like GeezJobs)
JINA_API_KEY=...          # jina.ai → free key raises relay limit to 200/day

# Anti-bot backends (for cloudflare_rotate and relay_rotate fallback)
SCRAPE_DO_API_KEY=...     # scrape.do → Dashboard → API Token (1 credit/req)
SCRAPEBADGER_API_KEY=...  # scrapebadger.com → Dashboard → API Key (1-3 credits/req)
ZENROWS_API_KEY=...       # zenrows.com → Dashboard → API Keys (25 credits/req)
SCRAPERAPI_KEY=...        # scraperapi.com → Dashboard → API Key (5-75 credits/req)
SCRAPFLY_API_KEY=...      # scrapfly.io → Dashboard → API Keys (30-80 credits/req)
```

Keys are stored as GitHub Actions secrets AND in `.env` for local dev.

---

## 10. Monitoring

- **Telegram report**: shows "⚠️ source_name: N item(s) failed" when a
  Cloudflare site partially fails, or "❌ source_name failed: Blocked by
  Cloudflare" when fully blocked
- **Admin dashboard**: `ScraperCreditUsage` table shows per-service usage
- **Day log**: `ScrapeLog.websites[source].status` shows success/partial/failed
- **Scraper logs**: each rotation attempt logs which service was tried and
  whether it succeeded
