"""Standalone inspection script for the Afriwork GraphQL API.

Fetches a single page of job listings and prints the raw response as
formatted JSON so the response structure can be studied before writing
the Django scraper.

Usage:
    python scripts/inspect_afriwork.py [limit] [offset]
"""
from __future__ import annotations

import json
import sys

import httpx

# Force UTF-8 stdout so Amharic/Unicode job text prints on Windows consoles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ENDPOINT = "https://api.afriworket.com/v1/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "x-hasura-role": "anonymous",
}

QUERY = """
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
"""


def fetch_jobs(limit: int = 5, offset: int = 0) -> dict:
    """POST the GraphQL query and return the parsed JSON response."""
    payload = {
        "query": QUERY,
        "variables": {"limit": limit, "offset": offset},
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(ENDPOINT, json=payload, headers=HEADERS)
        response.raise_for_status()
        return response.json()


def main(limit: int = 5, offset: int = 0) -> None:
    data = fetch_jobs(limit, offset)
    print(f"Response for limit={limit}, offset={offset}:")
    print("=" * 72)
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    main(limit, offset)
