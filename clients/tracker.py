"""Read-only SQLite client for the Anthropic Hiring Tracker DB.

Opens the DB via a `file:...?mode=ro` URI so the SQLite driver itself enforces
read-only mode. Combined with the `:ro` Docker volume mount, this is defense in
depth: even if one layer were misconfigured, the other would still block writes.
"""

import sqlite3
from typing import Any


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user input doesn't act as a pattern.

    Pattern matches the project's own `web.py`. Used in conjunction with
    `LIKE ? ESCAPE '\\\\'` in queries.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TrackerDB:
    """Thin read-only wrapper around the tracker SQLite database."""

    def __init__(self, db_path: str = "/data/tracker.db"):
        self.db_path = db_path
        # uri=True lets us pass `mode=ro` so the driver opens the file
        # read-only at the OS level too.
        #
        # `immutable=1` is critical when the DB is on a read-only volume
        # mount: without it, SQLite tries to create a `-shm` sidecar for
        # WAL-mode shared memory, which fails with "unable to open
        # database file" on a `:ro` mount. `immutable=1` promises the
        # file will not change while the connection is open, so SQLite
        # skips the WAL/journal/shm machinery entirely.
        self._uri = f"file:{db_path}?mode=ro&immutable=1"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------
    # search_jobs
    # ------------------------------------------------------------

    def search_jobs(
        self,
        query: str,
        department: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Title LIKE search across comma-separated terms (OR-joined).

        Optional department filter (also a LIKE match for forgiveness).
        """
        terms = [
            f"%{_escape_like(t.strip())}%" for t in query.split(",") if t.strip()
        ]
        if not terms:
            return []

        clauses = " OR ".join(["j.title LIKE ? ESCAPE '\\'"] * len(terms))
        params: list[Any] = list(terms)

        where: list[str] = [f"({clauses})"]
        if active_only:
            where.append("j.is_active = 1")
        if department:
            where.append("d.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(department)}%")

        sql = f"""
            SELECT j.id, j.title, d.name AS department,
                   j.location_raw, j.absolute_url, j.first_seen
              FROM jobs j
              LEFT JOIN departments d ON j.department_id = d.id
             WHERE {' AND '.join(where)}
             ORDER BY j.first_seen DESC
             LIMIT 200
        """

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": r["id"],
                "title": r["title"],
                "department": r["department"] or "Unknown",
                "location": r["location_raw"],
                "url": r["absolute_url"],
                "first_seen": r["first_seen"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------
    # recent_changes
    # ------------------------------------------------------------

    def recent_changes(self, days: int = 7) -> dict[str, list[dict[str, Any]]]:
        """Jobs added (first_seen) and removed (removed_date) in the last N days."""
        days = max(1, min(int(days), 365))
        conn = self._connect()
        try:
            added = conn.execute(
                f"""
                SELECT j.id, j.title, d.name AS department,
                       j.first_seen, j.absolute_url
                  FROM jobs j
                  LEFT JOIN departments d ON j.department_id = d.id
                 WHERE j.first_seen >= date('now', '-{days} days')
                 ORDER BY j.first_seen DESC, j.id DESC
                """
            ).fetchall()
            removed = conn.execute(
                f"""
                SELECT j.id, j.title, d.name AS department,
                       j.removed_date, j.absolute_url
                  FROM jobs j
                  LEFT JOIN departments d ON j.department_id = d.id
                 WHERE j.removed_date IS NOT NULL
                   AND j.removed_date >= date('now', '-{days} days')
                 ORDER BY j.removed_date DESC, j.id DESC
                """
            ).fetchall()
        finally:
            conn.close()

        return {
            "days": days,
            "added": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "department": r["department"] or "Unknown",
                    "date": r["first_seen"],
                    "url": r["absolute_url"],
                }
                for r in added
            ],
            "removed": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "department": r["department"] or "Unknown",
                    "date": r["removed_date"],
                    "url": r["absolute_url"],
                }
                for r in removed
            ],
        }

    # ------------------------------------------------------------
    # compensation_for
    # ------------------------------------------------------------

    def compensation_for(self, role_pattern: str) -> list[dict[str, Any]]:
        """Match jobs by title pattern, return per-match comp.

        DB stores salary_min/max in CENTS, so we divide by 100 to return dollars.
        """
        if not role_pattern.strip():
            return []
        pattern = f"%{_escape_like(role_pattern.strip())}%"

        sql = """
            SELECT j.id, j.title, d.name AS department,
                   c.salary_min, c.salary_max, c.currency,
                   c.comp_type, c.raw_text
              FROM jobs j
              JOIN compensation c ON c.job_id = j.id
              LEFT JOIN departments d ON j.department_id = d.id
             WHERE j.title LIKE ? ESCAPE '\\'
             ORDER BY j.is_active DESC, c.salary_max DESC
             LIMIT 200
        """

        conn = self._connect()
        try:
            rows = conn.execute(sql, (pattern,)).fetchall()
        finally:
            conn.close()

        return [
            {
                "job_id": r["id"],
                "title": r["title"],
                "department": r["department"] or "Unknown",
                "salary_min": (r["salary_min"] // 100) if r["salary_min"] else None,
                "salary_max": (r["salary_max"] // 100) if r["salary_max"] else None,
                "currency": r["currency"] or "USD",
                "comp_type": r["comp_type"] or "annual",
                "raw_text": r["raw_text"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------
    # department_trends
    # ------------------------------------------------------------

    def department_trends(
        self,
        name: str | None = None,
        days: int = 30,
    ) -> dict[str, Any]:
        """Per-department active-job count over time.

        `daily_snapshots.departments_json` stores a JSON object keyed by
        department name -> active count. We parse it in Python rather than
        relying on JSON1 functions (portable across SQLite builds).
        """
        import json as _json

        days = max(1, min(int(days), 365))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT date, departments_json
                  FROM daily_snapshots
                 WHERE date >= date('now', '-{days} days')
                 ORDER BY date ASC
                """
            ).fetchall()
        finally:
            conn.close()

        # Filter (case-insensitive substring) if a name was supplied.
        match_term = name.lower().strip() if name else None
        series: dict[str, list[dict[str, Any]]] = {}

        for r in rows:
            try:
                payload = _json.loads(r["departments_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            for dept_name, count in payload.items():
                if match_term and match_term not in dept_name.lower():
                    continue
                series.setdefault(dept_name, []).append(
                    {"date": r["date"], "count": count}
                )

        return {
            "days": days,
            "filter": name,
            "departments": [
                {"name": dept, "series": pts}
                for dept, pts in sorted(series.items())
            ],
        }

    # ------------------------------------------------------------
    # active_alerts
    # ------------------------------------------------------------

    def active_alerts(self, severity: str | None = None) -> list[dict[str, Any]]:
        """Return unacknowledged alerts, optionally filtered by severity."""
        sql = (
            "SELECT id, triggered_at, alert_type, severity, message, acknowledged "
            "FROM alerts WHERE acknowledged = 0"
        )
        params: list[Any] = []
        if severity:
            sql += " AND lower(severity) = lower(?)"
            params.append(severity.strip())
        sql += " ORDER BY triggered_at DESC LIMIT 100"

        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": r["id"],
                "triggered_at": r["triggered_at"],
                "type": r["alert_type"],
                "severity": r["severity"],
                "message": r["message"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------
    # daily_summary
    # ------------------------------------------------------------

    def daily_summary(self, date: str | None = None) -> dict[str, Any] | None:
        """Daily snapshot for the given date (default: latest)."""
        import json as _json

        conn = self._connect()
        try:
            if date:
                # Validate the date string by parameterizing — never f-string.
                row = conn.execute(
                    """
                    SELECT date, total_active_jobs, jobs_added, jobs_removed,
                           departments_json, locations_json
                      FROM daily_snapshots
                     WHERE date = ?
                    """,
                    (date,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT date, total_active_jobs, jobs_added, jobs_removed,
                           departments_json, locations_json
                      FROM daily_snapshots
                     ORDER BY date DESC
                     LIMIT 1
                    """
                ).fetchone()
        finally:
            conn.close()

        if not row:
            return None

        try:
            departments = _json.loads(row["departments_json"] or "{}")
        except (TypeError, ValueError):
            departments = {}
        try:
            locations = _json.loads(row["locations_json"] or "{}")
        except (TypeError, ValueError):
            locations = {}

        return {
            "date": row["date"],
            "total_active_jobs": row["total_active_jobs"],
            "jobs_added": row["jobs_added"],
            "jobs_removed": row["jobs_removed"],
            "departments": departments,
            "locations": locations,
        }

    # ------------------------------------------------------------
    # db_stats
    # ------------------------------------------------------------

    def db_stats(self) -> dict[str, Any]:
        """Row counts per table plus the latest snapshot date."""
        tables = [
            "jobs",
            "departments",
            "offices",
            "job_offices",
            "job_departments",
            "job_locations",
            "compensation",
            "daily_snapshots",
            "weekly_metrics",
            "alerts",
            "schema_version",
        ]
        counts: dict[str, int] = {}
        conn = self._connect()
        try:
            for t in tables:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {t}"  # table names whitelisted above
                    ).fetchone()
                    counts[t] = row["c"] if row else 0
                except sqlite3.OperationalError:
                    counts[t] = -1  # table missing in this schema version
            latest = conn.execute(
                "SELECT MAX(date) AS d FROM daily_snapshots"
            ).fetchone()
            total_snaps = conn.execute(
                "SELECT COUNT(*) AS c FROM daily_snapshots"
            ).fetchone()
            active_jobs = conn.execute(
                "SELECT COUNT(*) AS c FROM jobs WHERE is_active = 1"
            ).fetchone()
        finally:
            conn.close()

        return {
            "table_row_counts": counts,
            "active_jobs": active_jobs["c"] if active_jobs else 0,
            "latest_snapshot_date": latest["d"] if latest and latest["d"] else None,
            "total_snapshots": total_snaps["c"] if total_snaps else 0,
            "db_path": self.db_path,
            "read_only": True,
        }
