"""The error contract holds even for failures nobody anticipated.

Every tool in server.py documents what it returns on failure, and the live
tools handle the failures they expect. These tests cover the ones neither
anticipated, which before `tool_guard` escaped the contract entirely:

* a non-JSON 200 from Greenhouse (JSONDecodeError is not an httpx error, so it
  was in none of the caught tuples)
* a sqlite3.Error from any cached-DB tool (none of the seven caught anything)

Importing server.py needs TRACKER_DB_PATH pointed at a real file before import,
since the module exits at import time when the DB is missing.
"""

import importlib
import json
import sqlite3

import httpx
import pytest

from tests.conftest import _build_snapshot


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    path = tmp_path_factory.mktemp("guard") / "tracker.db"
    _build_snapshot(str(path))
    import os

    os.environ["TRACKER_DB_PATH"] = str(path)
    module = importlib.import_module("server")
    return importlib.reload(module)


def _parsed(raw):
    """Every tool returns a JSON string. That is itself part of the contract."""
    assert isinstance(raw, str)
    return json.loads(raw)


async def test_non_json_200_stays_inside_the_contract(server, monkeypatch):
    """A 200 whose body is not JSON is the escape that motivated the guard.

    A captive portal, a CDN error page, or a truncated body all produce it, and
    .json() raises JSONDecodeError -- which is not an httpx exception and so was
    caught by nothing.
    """

    async def html_response(*args, **kwargs):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(server.greenhouse, "fetch_jobs", html_response)
    payload = _parsed(await server.live_jobs())

    assert "error" in payload
    assert "JSONDecodeError" in payload["detail"]


async def test_db_failure_stays_inside_the_contract(server, monkeypatch):
    """None of the seven cached-DB tools caught anything before this."""

    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(server.db, "search_jobs", broken)
    payload = _parsed(await server.search_jobs(query="engineer"))

    assert "error" in payload
    assert "search_jobs" in payload["error"]
    assert "DatabaseError" in payload["detail"]


async def test_the_guard_does_not_shadow_a_tool_s_own_handler(server, monkeypatch):
    """The specific handlers still run first and keep their richer messages.

    A 404 must stay "Job not found" with its job_id, not be flattened into the
    generic envelope. This is the regression that would make the guard a
    downgrade rather than a backstop.
    """

    async def not_found(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "404",
            request=httpx.Request("GET", "https://boards-api.greenhouse.io/x"),
            response=httpx.Response(404),
        )

    monkeypatch.setattr(server.greenhouse, "fetch_job_detail", not_found)
    payload = _parsed(await server.live_job_detail(job_id=42))

    assert payload["error"] == "Job not found"
    assert payload["job_id"] == 42


async def test_a_working_tool_is_untouched(server):
    """The guard must be invisible on the success path."""
    payload = _parsed(await server.search_jobs(query="engineer"))
    assert "error" not in payload
    assert payload["count"] >= 1


async def test_every_tool_is_guarded(server):
    """A tool added later without the decorator is the way this regresses.

    functools.wraps sets __wrapped__, so the presence of that attribute on the
    registered function is the check. Asserted over the live registry rather
    than by reading the source, so it covers tools this file never names.
    """
    tools = await server.mcp.list_tools()
    assert len(tools) == 10

    unguarded = [
        tool.name
        for tool in tools
        if not hasattr(getattr(tool, "fn", None), "__wrapped__")
    ]
    assert unguarded == []
