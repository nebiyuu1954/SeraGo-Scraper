"""Standalone inspection script for the HaHuJobs GraphQL API.

Fetches a single page of job listings and prints the raw response as
formatted JSON so the response structure can be studied before writing
the Django scraper. Accepts an optional date window to test the
server-side ``approved_on`` filter the scraper uses for today-only mode.

Usage:
    python scripts/inspect_hahujobs.py [limit] [offset] [from] [to]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

# Force UTF-8 stdout so Amharic/Unicode job text prints on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ENDPOINT = "https://graph.aggregator.hahu.jobs/v1/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://www.hahu.jobs",
    "Referer": "https://www.hahu.jobs/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}

# Same query the scraper uses: a server-side "today" window on approved_on
# ($from/$to), non-expired + not requested_to_delete, newest-approved first.
QUERY = """
query GetJobs($limit: Int, $offset: Int, $from: timestamptz, $to: timestamptz) {
  jobs: search_jobs(
    where: {
      _and: [
        {expired: {_eq: false}}
        {requested_to_delete: {_eq: false}}
        {approved_on: {_gte: $from, _lt: $to}}
      ]
    }
    order_by: {approved_on: desc}
    args: {}
    offset: $offset
    limit: $limit
  ) {
    id
    title
    total_web_view_count
    telegram_view_count
    total_view_count
    type
    max_years_of_experience
    years_of_experience
    summary
    salary
    deadline
    expired
    location
    source
    application_method
    application_url
    application_email
    number_of_applicants
    approved_on
    job_cities {
      city {
        name
        region {
          name
          id
        }
      }
    }
    entity {
      logo
      name
      id
    }
    sub_sector {
      name
      sector {
        name
        id
        icon_class
        icon_code
      }
    }
    area {
      address
      name
    }
    isco_08 {
      isco_08_code
      title_en
      title_am
    }
    soc_2010 {
      title
      onetsoc_code
    }
    esco_code
  }
}
"""


def default_window() -> tuple[str, str]:
    """Today's Addis Ababa day window, like the Django scraper would use.

    The scraper injects ``timezone.get_current_timezone()`` (set to
    Africa/Addis_Ababa in settings), so this script mirrors that instead of
    the machine's local timezone.
    """
    tz = ZoneInfo("Africa/Addis_Ababa")
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def fetch_jobs(limit: int = 5, offset: int = 0, date_from: str | None = None, date_to: str | None = None) -> dict:
    """POST the GraphQL query and return the parsed JSON response."""
    if not date_from or not date_to:
        date_from, date_to = default_window()
    payload = {
        "query": QUERY,
        "variables": {
            "limit": limit,
            "offset": offset,
            "from": date_from,
            "to": date_to,
        },
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(ENDPOINT, json=payload, headers=HEADERS)
        response.raise_for_status()
        return response.json()


def main(limit: int = 5, offset: int = 0, date_from: str | None = None, date_to: str | None = None) -> None:
    data = fetch_jobs(limit, offset, date_from, date_to)
    jobs = data.get("data", {}).get("jobs")
    print(f"Response for limit={limit}, offset={offset}, from={date_from or 'today'}, to={date_to or 'tomorrow'}:")
    print("=" * 72)
    if isinstance(jobs, list):
        print(f"jobs returned: {len(jobs)}")
        if jobs:
            approved = [j.get("approved_on", "")[:10] for j in jobs]
            print(f"approved_on (first 10 chars): {approved}")
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    date_from = sys.argv[3] if len(sys.argv) > 3 else None
    date_to = sys.argv[4] if len(sys.argv) > 4 else None
    main(limit, offset, date_from, date_to)
