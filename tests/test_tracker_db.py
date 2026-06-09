"""Tests for the read-only SQLite query layer (clients/tracker.py).

Covers the risky bits: read-only enforcement, LIKE-wildcard escaping,
day-range clamping bounds, and that normal queries return shaped rows.
"""

import sqlite3

import pytest

from clients.tracker import TrackerDB, _escape_like


# ---------------------------------------------------------------------------
# read-only enforcement
# ---------------------------------------------------------------------------


def test_connection_is_read_only(snapshot_db):
    db = TrackerDB(snapshot_db)
    conn = db._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO jobs (id, title) VALUES (999, 'hack')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE jobs SET title = 'x' WHERE id = 1")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM jobs WHERE id = 1")
    finally:
        conn.close()


def test_uri_declares_ro_and_immutable(snapshot_db):
    db = TrackerDB(snapshot_db)
    assert "mode=ro" in db._uri
    assert "immutable=1" in db._uri


# ---------------------------------------------------------------------------
# _escape_like
# ---------------------------------------------------------------------------


def test_escape_like_escapes_wildcards():
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    assert _escape_like("back\\slash") == "back\\\\slash"
    # order matters: backslash must be escaped first so it doesn't double-escape
    assert _escape_like("%_") == "\\%\\_"


def test_search_jobs_like_wildcard_is_literal(snapshot_db):
    db = TrackerDB(snapshot_db)
    # "%" must match a literal percent sign, NOT act as a wildcard. If escaping
    # were broken, searching "%" alone would match every title.
    results = db.search_jobs("%")
    titles = [r["title"] for r in results]
    assert titles == ["50% Time Researcher"]  # only the row with a literal %


def test_search_jobs_underscore_is_literal(snapshot_db):
    db = TrackerDB(snapshot_db)
    # "_" is a single-char wildcard in LIKE; escaped it should match nothing here
    # because no title contains a literal underscore.
    assert db.search_jobs("_") == []


# ---------------------------------------------------------------------------
# search_jobs behavior
# ---------------------------------------------------------------------------


def test_search_jobs_basic_match(snapshot_db):
    db = TrackerDB(snapshot_db)
    rows = db.search_jobs("Engineer")
    titles = {r["title"] for r in rows}
    assert "Research Engineer" in titles
    assert "Security Engineer" in titles
    # shape check
    r = next(r for r in rows if r["title"] == "Research Engineer")
    assert r["department"] == "Research"
    assert r["url"] == "https://jobs/1"
    assert set(r.keys()) == {
        "id", "title", "department", "location", "url", "first_seen",
    }


def test_search_jobs_active_only_filters_inactive(snapshot_db):
    db = TrackerDB(snapshot_db)
    active = db.search_jobs("Security", active_only=True)
    assert all(r["title"] != "Old Security Analyst" for r in active)
    inactive_too = db.search_jobs("Security", active_only=False)
    titles = {r["title"] for r in inactive_too}
    assert "Old Security Analyst" in titles


def test_search_jobs_comma_terms_or_joined(snapshot_db):
    db = TrackerDB(snapshot_db)
    rows = db.search_jobs("Research, Mystery")
    titles = {r["title"] for r in rows}
    assert "Research Engineer" in titles
    assert "Mystery Role" in titles


def test_search_jobs_null_department_is_unknown(snapshot_db):
    db = TrackerDB(snapshot_db)
    rows = db.search_jobs("Mystery")
    assert rows[0]["department"] == "Unknown"


def test_search_jobs_empty_query_returns_empty(snapshot_db):
    db = TrackerDB(snapshot_db)
    assert db.search_jobs("   ") == []
    assert db.search_jobs(",, ,") == []


def test_search_jobs_department_filter(snapshot_db):
    db = TrackerDB(snapshot_db)
    rows = db.search_jobs("Engineer", department="Research")
    titles = {r["title"] for r in rows}
    assert titles == {"Research Engineer"}


# ---------------------------------------------------------------------------
# day clamping (recent_changes / department_trends)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (0, 1),        # below floor -> clamped up to 1
        (-50, 1),      # negative -> 1
        (7, 7),        # normal passthrough
        (365, 365),    # at ceiling
        (10000, 365),  # above ceiling -> clamped down to 365
    ],
)
def test_recent_changes_day_clamping(snapshot_db, given, expected):
    db = TrackerDB(snapshot_db)
    out = db.recent_changes(days=given)
    assert out["days"] == expected


def test_recent_changes_string_days_coerced(snapshot_db):
    db = TrackerDB(snapshot_db)
    out = db.recent_changes(days="30")  # type: ignore[arg-type]
    assert out["days"] == 30


def test_department_trends_day_clamping(snapshot_db):
    db = TrackerDB(snapshot_db)
    assert db.department_trends(days=0)["days"] == 1
    assert db.department_trends(days=99999)["days"] == 365


# ---------------------------------------------------------------------------
# other read paths return shaped data
# ---------------------------------------------------------------------------


def test_compensation_for_cents_to_dollars(snapshot_db):
    db = TrackerDB(snapshot_db)
    rows = db.compensation_for("Research Engineer")
    assert len(rows) == 1
    r = rows[0]
    assert r["salary_min"] == 290000   # 29_000_000 cents / 100
    assert r["salary_max"] == 435000
    assert r["currency"] == "USD"
    assert r["comp_type"] == "annual"


def test_compensation_for_empty_pattern(snapshot_db):
    db = TrackerDB(snapshot_db)
    assert db.compensation_for("   ") == []


def test_department_trends_series(snapshot_db):
    db = TrackerDB(snapshot_db)
    out = db.department_trends(name="Research", days=365)
    assert out["filter"] == "Research"
    depts = {d["name"]: d for d in out["departments"]}
    assert "Research" in depts
    assert "Security" not in depts  # filtered out by name
    # series ordered by date asc, both snapshots present
    pts = depts["Research"]["series"]
    assert [p["date"] for p in pts] == ["2026-06-07", "2026-06-08"]


def test_active_alerts_unacked_only(snapshot_db):
    db = TrackerDB(snapshot_db)
    alerts = db.active_alerts()
    ids = {a["id"] for a in alerts}
    assert ids == {1, 2}  # acked alert 3 excluded


def test_active_alerts_severity_filter_case_insensitive(snapshot_db):
    db = TrackerDB(snapshot_db)
    alerts = db.active_alerts(severity="high")
    assert {a["id"] for a in alerts} == {1}


def test_daily_summary_latest(snapshot_db):
    db = TrackerDB(snapshot_db)
    out = db.daily_summary()
    assert out["date"] == "2026-06-08"  # latest
    assert out["total_active_jobs"] == 5
    assert out["departments"] == {"Research": 2, "Security": 2}


def test_daily_summary_specific_date_parameterized(snapshot_db):
    db = TrackerDB(snapshot_db)
    out = db.daily_summary(date="2026-06-07")
    assert out["date"] == "2026-06-07"
    # an injection attempt in the date param is treated as a literal value,
    # finds no row, and returns None (no error, no extra rows).
    assert db.daily_summary(date="2026-06-07' OR '1'='1") is None


def test_db_stats_reports_read_only_and_counts(snapshot_db):
    db = TrackerDB(snapshot_db)
    stats = db.db_stats()
    assert stats["read_only"] is True
    assert stats["table_row_counts"]["jobs"] == 5
    assert stats["active_jobs"] == 4
    assert stats["latest_snapshot_date"] == "2026-06-08"
    # a table absent from this minimal fixture is reported as -1, not an error
    assert stats["table_row_counts"]["weekly_metrics"] == -1
