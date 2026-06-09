"""Shared fixtures: a tiny on-disk SQLite snapshot matching the tracker schema.

We build the DB read-write to seed it, close it, then hand the path to
TrackerDB which re-opens it via `file:...?mode=ro&immutable=1`. The tests
exercise the read path exactly as production does.
"""

import sqlite3

import pytest


def _build_snapshot(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE departments (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT,
            department_id INTEGER,
            location_raw TEXT,
            absolute_url TEXT,
            first_seen TEXT,
            removed_date TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE compensation (
            job_id INTEGER,
            salary_min INTEGER,
            salary_max INTEGER,
            currency TEXT,
            comp_type TEXT,
            raw_text TEXT
        );
        CREATE TABLE daily_snapshots (
            date TEXT PRIMARY KEY,
            total_active_jobs INTEGER,
            jobs_added INTEGER,
            jobs_removed INTEGER,
            departments_json TEXT,
            locations_json TEXT
        );
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            triggered_at TEXT,
            alert_type TEXT,
            severity TEXT,
            message TEXT,
            acknowledged INTEGER DEFAULT 0
        );
        """
    )
    conn.executemany(
        "INSERT INTO departments (id, name) VALUES (?, ?)",
        [(1, "Research"), (2, "Security"), (3, "Go-To-Market")],
    )
    conn.executemany(
        """INSERT INTO jobs
           (id, title, department_id, location_raw, absolute_url,
            first_seen, removed_date, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            # recent active research role
            (1, "Research Engineer", 1, "San Francisco",
             "https://jobs/1", "2026-06-07", None, 1),
            # 100% match for LIKE-wildcard escaping test (title contains a literal %)
            (2, "50% Time Researcher", 1, "Remote",
             "https://jobs/2", "2026-06-06", None, 1),
            # an inactive role (filtered out when active_only=True)
            (3, "Old Security Analyst", 2, "London",
             "https://jobs/3", "2025-01-01", "2026-06-05", 0),
            # active security role, recent
            (4, "Security Engineer", 2, "New York",
             "https://jobs/4", "2026-06-08", None, 1),
            # a job with a NULL department (LEFT JOIN -> "Unknown")
            (5, "Mystery Role", None, "Nowhere",
             "https://jobs/5", "2026-06-08", None, 1),
        ],
    )
    conn.executemany(
        """INSERT INTO compensation
           (job_id, salary_min, salary_max, currency, comp_type, raw_text)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            # cents -> dollars (29000000 cents == $290,000)
            (1, 29000000, 43500000, "USD", "annual", "$290,000-$435,000 USD"),
            (4, None, None, None, None, None),
        ],
    )
    conn.executemany(
        """INSERT INTO daily_snapshots
           (date, total_active_jobs, jobs_added, jobs_removed,
            departments_json, locations_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("2026-06-07", 4, 1, 0,
             '{"Research": 2, "Security": 1}', '{"San Francisco": 2}'),
            ("2026-06-08", 5, 1, 0,
             '{"Research": 2, "Security": 2}', '{"New York": 1}'),
        ],
    )
    conn.executemany(
        """INSERT INTO alerts
           (id, triggered_at, alert_type, severity, message, acknowledged)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (1, "2026-06-08T10:00:00", "new_job", "HIGH", "New research role", 0),
            (2, "2026-06-07T09:00:00", "removed", "low", "Role removed", 0),
            (3, "2026-06-06T08:00:00", "noise", "low", "Acked", 1),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def snapshot_db(tmp_path):
    """Path to a freshly built read-only-ready snapshot DB."""
    path = str(tmp_path / "tracker.db")
    _build_snapshot(path)
    return path
