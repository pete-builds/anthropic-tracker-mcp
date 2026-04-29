"""MCP Anthropic Tracker - access to Anthropic hiring data.

Two data sources, ten tools total:

* 7 cached-DB tools (read-only SQLite mount, populated by the nightly
  `anthropic-tracker` cron container on nix1).
* 3 live tools that hit Greenhouse's public board API directly so you can
  bypass the cache when freshness matters.

The DB is mounted read-only at the volume layer (`anthropic-tracker-data:/data:ro`)
and opened read-only at the SQLite driver layer (`file:...?mode=ro`) for defense
in depth. The live tools never touch the DB.
"""

import json
import os
import sys

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

from clients.greenhouse import (
    GreenhouseClient,
    build_department_map,
    enrich_jobs_with_departments,
)
from clients.parser import parse_compensation
from clients.tracker import TrackerDB, _escape_like

load_dotenv()

# --- Config ---
DB_PATH = os.getenv("TRACKER_DB_PATH", "/data/tracker.db")

if not os.path.exists(DB_PATH):
    print(
        f"FATAL: tracker DB not found at {DB_PATH}.\n"
        "Mount the `anthropic-tracker-data` volume read-only at /data:\n"
        "  -v anthropic-tracker-data:/data:ro\n",
        file=sys.stderr,
    )
    sys.exit(1)

db = TrackerDB(DB_PATH)

# Single Greenhouse client reused across tool calls for connection pooling.
greenhouse = GreenhouseClient()

# --- MCP Server ---
mcp = FastMCP("Anthropic Tracker")


def _format(data: object) -> str:
    """Format response data as readable JSON string."""
    return json.dumps(data, indent=2, default=str)


def _greenhouse_error(exc: Exception) -> dict:
    """Shape an httpx exception into a structured tool response.

    Tools must NEVER raise. Raising would crash the MCP session for Claude.
    """
    return {
        "error": "Greenhouse API unavailable",
        "detail": f"{type(exc).__name__}: {exc}",
    }


# ============================================================
# Cached DB tools (read-only)
# ============================================================


@mcp.tool()
async def search_jobs(
    query: str,
    department: str | None = None,
    active_only: bool = True,
) -> str:
    """Search Anthropic job postings by title.

    Comma-separated terms are OR-joined (e.g. "engineer, manager" matches
    either). Optional department filter (substring match, case-insensitive).

    Args:
        query: Comma-separated title search terms. Required.
        department: Optional department name substring (e.g. "Research").
        active_only: If True (default), only currently-active jobs are returned.

    Returns:
        JSON list of matching jobs with id, title, department, location, url, first_seen.
    """
    results = db.search_jobs(query, department=department, active_only=active_only)
    return _format({"count": len(results), "jobs": results})


@mcp.tool()
async def recent_changes(days: int = 7) -> str:
    """List jobs added and removed in the last N days.

    Args:
        days: Lookback window in days (default 7, max 365).

    Returns:
        JSON with `added` and `removed` arrays of jobs, each tagged with the
        first_seen / removed_date that triggered inclusion.
    """
    return _format(db.recent_changes(days=days))


@mcp.tool()
async def compensation_for(role_pattern: str) -> str:
    """Look up posted compensation for jobs matching a title pattern.

    Salaries returned in dollars (DB stores cents internally).

    Args:
        role_pattern: Substring of the job title (e.g. "research engineer").

    Returns:
        JSON list of jobs with min/max salary, currency, comp_type, raw_text.
    """
    matches = db.compensation_for(role_pattern)
    return _format({"pattern": role_pattern, "count": len(matches), "matches": matches})


@mcp.tool()
async def department_trends(name: str | None = None, days: int = 30) -> str:
    """Per-department active job counts over the last N days.

    Args:
        name: Optional department-name substring filter (case-insensitive).
              If omitted, returns every department in the snapshots.
        days: Lookback window in days (default 30, max 365).

    Returns:
        JSON with each department's date->count series sorted ascending by date.
    """
    return _format(db.department_trends(name=name, days=days))


@mcp.tool()
async def active_alerts(severity: str | None = None) -> str:
    """List unacknowledged tracker alerts.

    Args:
        severity: Optional filter — one of "info", "warning", "critical".

    Returns:
        JSON list of alerts (id, triggered_at, type, severity, message).
    """
    alerts = db.active_alerts(severity=severity)
    return _format({"count": len(alerts), "alerts": alerts})


@mcp.tool()
async def daily_summary(date: str | None = None) -> str:
    """Snapshot of total/added/removed jobs and per-department/location counts.

    Args:
        date: ISO date (YYYY-MM-DD). Default: latest available snapshot.

    Returns:
        JSON with date, total_active_jobs, jobs_added, jobs_removed, departments,
        locations. Returns `null` if no snapshot exists for that date.
    """
    summary = db.daily_summary(date=date)
    if summary is None:
        return _format({"date": date, "found": False})
    return _format(summary)


@mcp.tool()
async def db_stats() -> str:
    """Database health snapshot: row counts per table, latest snapshot, totals.

    Returns:
        JSON with table_row_counts, active_jobs, latest_snapshot_date,
        total_snapshots, db_path, read_only.
    """
    return _format(db.db_stats())


# ============================================================
# Live Greenhouse tools (no cache, direct API)
# ============================================================


def _filter_live_jobs(
    jobs: list[dict],
    query: str | None,
    department: str | None,
) -> list[dict]:
    """Apply in-memory filters on a fetched job list.

    `query`: comma-separated terms, OR semantics, case-insensitive substring
             match against `title`. `_escape_like` is applied for consistency
             with the DB tools, even though we're matching in Python.
    `department`: exact match against any of the job's `departments[*].name`.
    """
    terms: list[str] = []
    if query:
        terms = [
            _escape_like(t.strip()).lower()
            for t in query.split(",")
            if t.strip()
        ]

    out: list[dict] = []
    for job in jobs:
        title = (job.get("title") or "").lower()
        if terms and not any(t in title for t in terms):
            continue
        if department:
            dept_names = [d.get("name") for d in (job.get("departments") or [])]
            if department not in dept_names:
                continue
        out.append(job)
    return out


def _shape_live_job(job: dict) -> dict:
    """Shape a Greenhouse job dict to match the DB tools' output shape."""
    departments = job.get("departments") or []
    dept_name = departments[0]["name"] if departments else "Unknown"
    location = job.get("location") or {}
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "department": dept_name,
        "location_raw": location.get("name"),
        "absolute_url": job.get("absolute_url"),
        "first_published": job.get("first_published"),
    }


@mcp.tool()
async def live_jobs(
    query: str | None = None,
    department: str | None = None,
) -> str:
    """Fetch the live Anthropic job list directly from Greenhouse.

    Bypasses the nightly DB snapshot. Use this when you need the absolute
    latest postings (within minutes of being added/removed) instead of the
    cached `search_jobs` view.

    Args:
        query: Optional comma-separated title search terms (OR semantics,
               case-insensitive substring match against the job title).
        department: Optional exact match against the job's department name(s).

    Returns:
        JSON list of {id, title, department, location_raw, absolute_url,
        first_published}. On API failure: {error, detail}.
    """
    try:
        jobs = await greenhouse.fetch_jobs()
        # The bare /jobs endpoint omits department data. Pull /departments
        # separately and stitch the two together so the `department` filter
        # has something to match against.
        departments = await greenhouse.fetch_departments()
    except (
        httpx.HTTPStatusError,
        httpx.TransportError,
        httpx.TimeoutException,
    ) as exc:
        return _format(_greenhouse_error(exc))

    enrich_jobs_with_departments(jobs, build_department_map(departments))

    filtered = _filter_live_jobs(jobs, query, department)
    shaped = [_shape_live_job(j) for j in filtered]
    return _format({"count": len(shaped), "jobs": shaped})


@mcp.tool()
async def live_job_detail(job_id: int) -> str:
    """Fetch a single Anthropic job with full HTML content from Greenhouse.

    Args:
        job_id: Greenhouse job ID.

    Returns:
        JSON {id, title, department, location, content, absolute_url}.
        On 404: {error: "Job not found", job_id}.
        On API failure: {error: "Greenhouse API unavailable", detail}.
    """
    try:
        job = await greenhouse.fetch_job_detail(job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _format({"error": "Job not found", "job_id": job_id})
        return _format(_greenhouse_error(exc))
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return _format(_greenhouse_error(exc))

    departments = job.get("departments") or []
    dept_name = departments[0]["name"] if departments else "Unknown"
    location = job.get("location") or {}

    return _format({
        "id": job.get("id"),
        "title": job.get("title"),
        "department": dept_name,
        "location": location.get("name"),
        "content": job.get("content") or "",
        "absolute_url": job.get("absolute_url"),
    })


@mcp.tool()
async def live_compensation(job_id: int) -> str:
    """Parse compensation directly from a live Greenhouse job description.

    Hits the single-job endpoint, runs the same parser the nightly tracker
    uses against the returned HTML, and returns dollar-denominated values.

    Args:
        job_id: Greenhouse job ID.

    Returns:
        JSON {job_id, salary_min, salary_max, currency, comp_type, raw_text}
        on success (salaries in DOLLARS).
        On no salary detected: {job_id, compensation: null}.
        On 404: {error: "Job not found", job_id}.
        On API failure: {error: "Greenhouse API unavailable", detail}.
    """
    try:
        job = await greenhouse.fetch_job_detail(job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _format({"error": "Job not found", "job_id": job_id})
        return _format(_greenhouse_error(exc))
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return _format(_greenhouse_error(exc))

    parsed = parse_compensation(job.get("content") or "")
    if not parsed:
        return _format({"job_id": job_id, "compensation": None})

    # Parser returns CENTS — surface as dollars to match `compensation_for`.
    return _format({
        "job_id": job_id,
        "salary_min": parsed["salary_min"] // 100 if parsed.get("salary_min") else None,
        "salary_max": parsed["salary_max"] // 100 if parsed.get("salary_max") else None,
        "currency": parsed.get("currency", "USD"),
        "comp_type": parsed.get("comp_type", "annual"),
        "raw_text": parsed.get("raw_text"),
    })


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "3713"))
    print(f"Starting MCP Anthropic Tracker on {host}:{port} (SSE transport)")
    print(f"DB (read-only): {DB_PATH}")
    print("Live source: https://boards-api.greenhouse.io/v1/boards/anthropic")
    mcp.run(transport="sse", host=host, port=port)
