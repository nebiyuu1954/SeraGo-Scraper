# SeraGo – Ethiopian Job Scraping Service

## Project Overview
SeraGo is a standalone Django scraping service that autonomously fetches, parses, normalizes, and stores job listings from multiple Ethiopian job websites. It runs independently of your main .NET backend, sharing only the PostgreSQL database.

**Key Principles:**
- **Decoupled** – No direct calls from .NET; only database as the contract.
- **Configuration‑driven** – Adding a new source requires zero code changes.
- **Resilient** – Automatic retries, error logging, and health checks.
- **Observable** – Admin dashboard and structured logs for debugging.

## System Architecture
┌─────────────────────────────────────────────────────────────────────┐
│ SeraGo (Django) │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Django Admin (manage sources, view logs, trigger manually) │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Celery Beat (scheduler – triggers tasks at intervals) │ │
│ └────────────────────────────┬─────────────────────────────────┘ │
│ │ sends tasks │
│ ┌────────────────────────────▼─────────────────────────────────┐ │
│ │ Celery Workers (execute scraping tasks) │ │
│ │ - fetch, parse, normalize, deduplicate, store │ │
│ │ - health check tasks │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ │
│ ┌──────────────────────────────────────────────────────────────┐ │
│ │ Django Models (shared with .NET) │ │
│ │ - Source, ScrapedItem, ScrapeLog, HealthResult │ │
│ └──────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
│
│ database (shared)
▼
┌─────────────────────────────────────────────────────────────────────┐
│ PostgreSQL (Neon) │
│ tables: sources, scraped_items, scrape_logs, health_results │
└─────────────────────────────────────────────────────────────────────┘
▲
│
┌─────────────────────────────────────────────────────────────────────┐
│ .NET Backend (separate service) │
│ - reads jobs, sources, logs │
│ - serves frontend API │
└─────────────────────────────────────────────────────────────────────┘


## Technology Stack

| Component | Technology |
|-----------|------------|
| Python version | 3.12+ |
| Web framework | Django 5.0+ |
| Task queue | Celery 5.3+ |
| Broker/result backend | Redis 7.0+ |
| Database | PostgreSQL (via Neon) |
| HTTP client | httpx |
| HTML parsing | BeautifulSoup4 + lxml |
| JSON parsing | orjson |
| Headless browser | Playwright (optional fallback) |
| Scheduling | Celery Beat |
| Monitoring | Flower (optional) |
| Environment | python-dotenv |

## Target Websites Inventory

All websites are public and do not require authentication for listing jobs.

### 1. Afriwork (Freelance Ethiopia)
- **URL**: https://afriworket.com/jobs
- **Scraper Type**: Public GraphQL API (Hasura)
- **API Endpoint**: https://api.afriworket.com/v1/graphql
- **Method**: POST
- **Headers**: `Content-Type: application/json`, `x-hasura-role: anonymous`
- **Pagination**: Offset-based with `offset` and `limit` (10)
- **Authentication**: None (anonymous role)
- **Notes**: Clean JSON response. No browser automation needed. Powered by Hasura.

### 2. Ethiojobs.net
- **URL**: https://ethiojobs.net/jobs
- **Scraper Type**: Public Next.js Data API
- **API Endpoint**: https://ethiojobs.net/_next/data/{build_id}/en/jobs.json
- **Method**: GET
- **Headers**: `Accept: application/json`
- **Pagination**: Query parameter `?page={page_number}&isFeatured=false`
- **Authentication**: None
- **Notes**: Straightforward JSON API. Build ID is static per deployment.

### 3. HaHu Jobs
- **URL**: https://www.hahu.jobs
- **Scraper Type**: Public GraphQL API (Hasura)
- **API Endpoint**: https://graph.aggregator.hahu.jobs/v1/graphql
- **Method**: POST
- **Headers**: `Content-Type: application/json`, `x-hasura-role: anonymous`
- **Pagination**: Offset-based with `offset` and `limit` (10). Response includes `aggregate.count`.
- **Authentication**: None (anonymous role)
- **Notes**: Clean JSON response. No browser automation needed. Powered by Hasura.

### 4. GeezJobs
- **URL**: https://geezjobs.com/search-jobs?page=2
- **Scraper Type**: Server-Side Rendered HTML
- **Request URL**: https://geezjobs.com/search-jobs?page=2
- **Method**: GET
- **Data Retrieval**: Job listings are embedded in HTML with JSON-LD structured data in script tags.
- **Pagination**: URL parameter `page`
- **Authentication**: None
- **Notes**: No headless browser needed. Direct HTTP GET with BeautifulSoup parsing works.

### 5. Ethiopian Reporter Jobs
- **URL**: https://www.ethiopianreporterjobs.com/jobs-in-ethiopia/page/2/
- **Scraper Type**: Server-Side Rendered HTML (WordPress)
- **Method**: GET
- **Data Retrieval**: HTML with structured classes. WordPress job board theme.
- **Pagination**: URL path with `/page/{number}/`
- **Authentication**: None
- **Notes**: No browser automation needed. Parse with BeautifulSoup.

## Architecture Implementation Flow

1. **Django Admin** stores source configurations (endpoint, headers, query, selectors, field mapping, pagination rules, intervals).
2. **Celery Beat** schedules scraping tasks based on each source's `scrape_interval_hours`.
3. **Celery Workers** execute `scrape_source` tasks.
4. Each task uses a **ScraperFactory** to instantiate the appropriate scraper type (GraphQL, HTML, Next.js, Playwright).
5. The scraper **fetches** data (HTTP or headless browser).
6. Data is **parsed** into a list of raw dictionaries.
7. The parser **normalizes** using the source's `field_mapping` (dotted paths).
8. A **SHA‑256 hash** is computed (title + company + location + job_type).
9. **Deduplication** ensures each source+external_id is unique.
10. **ScrapeLog** is created for audit (status, items found, inserted, duration, errors).
11. **Health checks** periodically validate that sources are still accessible.

## First Implementation Step

**Start with Afriwork GraphQL API.**
- Endpoint: `https://api.afriworket.com/v1/graphql`
- Use the query:
  ```graphql
  query GetJobs($limit: Int, $offset: Int) {
    jobs(limit: $limit, offset: $offset) {
      id
      title
      description
      location
      job_type
      published_at
      created_at
      deadline
    }
  }