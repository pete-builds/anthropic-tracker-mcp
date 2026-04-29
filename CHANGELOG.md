# Changelog

## 2026-04-29 — Live Greenhouse tools (3 added, 10 total)

Added `live_jobs`, `live_job_detail`, `live_compensation` for queries that need data fresher than the daily cron's last snapshot.

- New: `clients/greenhouse.py` — async httpx client with exponential-backoff retries (1s, 2s; max 2 retries; 30s timeout; UA `anthropic-tracker-mcp/0.1.0`).
- New: `clients/parser.py` — copy of the upstream tracker's compensation parser. One small divergence vs upstream: `parse_compensation()` now `html.unescape()`s its input. The upstream tracker hits the bulk `/jobs?content=true` endpoint which returns raw HTML, but this MCP's `live_compensation` calls the per-job `/jobs/{id}` endpoint which returns HTML-entity-encoded content (`&lt;div&gt;...`). Without `html.unescape`, the parser silently returned `None` on every live job. Flagged in the file header so anyone re-syncing from upstream knows about the divergence.
- The bare `/jobs` endpoint omits department data; only `/departments` has it. `live_jobs` calls both and stitches with `enrich_jobs_with_departments()` so the `department` filter has data to match on.
- Bumped `requirements.txt` with `httpx>=0.27,<1` and `beautifulsoup4>=4.12,<5`.

## Initial release — 7 cached DB tools

- `search_jobs`, `recent_changes`, `compensation_for`, `department_trends`, `active_alerts`, `daily_summary`, `db_stats`.
- Read-only mount + `mode=ro&immutable=1` SQLite URI. No write code paths anywhere.
- FastMCP + SSE transport on port 3713, single Docker container, `unless-stopped` restart policy.
