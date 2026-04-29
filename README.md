# anthropic-tracker-mcp

MCP server that exposes Anthropic's hiring data to Claude Code (and any other MCP client). Two data sources, ten tools.

- **7 cached tools** read from a read-only SQLite mount populated by [`anthropic-tracker`](https://github.com/pete-builds/anthropic-tracker)'s nightly cron. Fast, indexed, good for trend analysis.
- **3 live tools** hit Greenhouse's public board API directly so you can bypass the cache when freshness matters (within minutes of a posting going up).

The DB layer is read-only by design: the volume is mounted with `:ro` and the SQLite driver opens it via `file:...?mode=ro`. The live layer is read-only by nature: Greenhouse's public API doesn't accept writes.

## Architecture

```
                                   +--> /data/tracker.db (read-only URI)
                                   |       (cached, nightly snapshot)
Claude Code  --SSE-->  anthropic-tracker-mcp (port 3713)
                                   |
                                   +--> boards-api.greenhouse.io
                                           (live, on-demand)
```

The same named volume `anthropic-tracker-data` is mounted read-write by the [`anthropic-tracker`](https://github.com/pete-builds/anthropic-tracker) cron container and read-only here. The MCP server cannot mutate state, even if the code tried to.

## Tools (10)

### Cached DB (7)

| Tool | Description |
|------|-------------|
| `search_jobs(query, department=None, active_only=True)` | Title LIKE search (comma = OR), optional department filter |
| `recent_changes(days=7)` | Jobs added + removed in the last N days |
| `compensation_for(role_pattern)` | Posted comp for jobs matching a title pattern (dollars) |
| `department_trends(name=None, days=30)` | Per-department active count over time |
| `active_alerts(severity=None)` | Unacknowledged alerts, optional severity filter |
| `daily_summary(date=None)` | Snapshot for one date (default: latest) |
| `db_stats()` | Row counts per table, latest snapshot, totals |

### Live Greenhouse (3)

| Tool | Description |
|------|-------------|
| `live_jobs(query=None, department=None)` | Fresh job list pulled directly from Greenhouse. Optional title search (comma = OR) and exact-match department filter. |
| `live_job_detail(job_id)` | Full job posting (id, title, department, location, raw HTML content, url) for a single Greenhouse job ID. |
| `live_compensation(job_id)` | Parse comp directly from the live HTML. Salaries in dollars. Returns `{compensation: null}` when no salary is published. |

All live tools return structured `{error, detail}` dicts on API failure (never raise — that would crash the MCP session). A 404 returns `{error: "Job not found", job_id}` distinctly.

All user input that flows into `LIKE` clauses (DB tools) is escaped via `_escape_like()` and parameterized — never f-stringed into the SQL. The same escape is applied to live `query` terms for consistency, even though matching happens in Python.

## Stack

- Python 3.13 + FastMCP 3.1.0 (SSE transport)
- SQLite (stdlib `sqlite3`, read-only URI mode)
- httpx (async, for live Greenhouse calls)
- beautifulsoup4 (HTML parsing for live compensation)
- Docker container, port 3713

## Quick start

You need the `anthropic-tracker-data` Docker volume to already exist — that's created and populated by the [`anthropic-tracker`](https://github.com/pete-builds/anthropic-tracker) project. Stand that up first.

```bash
git clone https://github.com/pete-builds/anthropic-tracker-mcp.git
cd anthropic-tracker-mcp
docker compose up -d --build
docker logs anthropic-tracker-mcp     # confirm clean startup
```

The MCP server is now serving SSE at `http://localhost:3713/sse`.

## Register with Claude Code

```bash
claude mcp add anthropic-tracker --transport sse --scope user \
  --url http://<host>:3713/sse
```

Replace `<host>` with `localhost` if you registered on the same machine, or whatever address points to the container otherwise (LAN, Tailscale, reverse proxy).

## File structure

```
anthropic-tracker-mcp/
  server.py              # FastMCP app — 10 @mcp.tool() definitions
  healthcheck.py         # Docker HEALTHCHECK
  clients/
    __init__.py
    tracker.py           # TrackerDB: read-only sqlite wrapper + queries
    greenhouse.py        # GreenhouseClient: async httpx + retry
    parser.py            # Compensation parser (synced from upstream tracker)
  Dockerfile             # python:3.13-slim, pinned digest
  docker-compose.yml     # Port 3713, RO volume mount, host network
  requirements.txt
  .env.example
  CHANGELOG.md
  LICENSE
```

## Configuration

Environment variables (all optional):

| Var | Default | Purpose |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `3713` | Bind port |
| `TRACKER_DB_PATH` | `/data/tracker.db` | SQLite path inside the container |
| `TZ` | `America/New_York` | Container timezone |

## Read-only by design (defense in depth)

| Layer | Mechanism |
|-------|-----------|
| Docker | `volumes: anthropic-tracker-data:/data:ro` |
| SQLite driver | `sqlite3.connect("file:/data/tracker.db?mode=ro&immutable=1", uri=True)` |
| Code | No `INSERT/UPDATE/DELETE` statements anywhere; all tools return `dict`/`list` |
| Live tools | Only issue HTTP GETs against Greenhouse's public API |

If anyone tries `INSERT INTO alerts ...` inside the container, SQLite raises `OperationalError: attempt to write a readonly database`. The volume `:ro` mount also makes the file read-only at the kernel level. Three independent guarantees.

## Why `immutable=1` matters

The upstream `anthropic-tracker` writes the DB in WAL mode, which uses sidecar files (`-shm`, `-wal`) that SQLite needs to be able to create on the same filesystem as the DB. With our `:ro` mount, that creation fails. `immutable=1` tells SQLite the file will not change for the duration of this connection, so it skips the WAL/journal/shm machinery entirely. That's also the correct posture for a read-only consumer that should never see in-flight writes.

## Related

- [`anthropic-tracker`](https://github.com/pete-builds/anthropic-tracker) — the data pipeline (CLI, web dashboard, daily cron) that produces the SQLite DB this MCP serves

## License

MIT — see [LICENSE](LICENSE).
