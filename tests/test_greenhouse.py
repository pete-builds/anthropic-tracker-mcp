"""HTTP-mocked tests for the Greenhouse client (clients/greenhouse.py).

Covers: retry-on-transport-error-then-success, 4xx surfaces immediately
without retrying, and that int(job_id) rejects path-injection input. asyncio
sleep is patched so backoff doesn't actually wait.
"""

import httpx
import pytest
import respx

from clients import greenhouse as gh
from clients.greenhouse import (
    GREENHOUSE_API_URL,
    GreenhouseClient,
    build_department_map,
    enrich_jobs_with_departments,
)


@pytest.fixture
async def client(monkeypatch):
    # Don't actually sleep between retries.
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(gh.asyncio, "sleep", _no_sleep)
    c = GreenhouseClient()
    yield c
    await c.aclose()


# ---------------------------------------------------------------------------
# retry on transport error, then success
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_retries_transport_error_then_succeeds(client):
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Eng"}]})

    respx.get(GREENHOUSE_API_URL).mock(side_effect=_handler)

    jobs = await client.fetch_jobs()
    assert jobs == [{"id": 1, "title": "Eng"}]
    assert calls["n"] == 3  # 2 failures + 1 success (MAX_RETRIES = 2)


@respx.mock
@pytest.mark.asyncio
async def test_retries_exhausted_raises_transport_error(client):
    respx.get(GREENHOUSE_API_URL).mock(
        side_effect=httpx.ConnectError("down")
    )
    with pytest.raises(httpx.TransportError):
        await client.fetch_jobs()


# ---------------------------------------------------------------------------
# 4xx/5xx surfaces immediately (no retry)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_404_surfaces_immediately_without_retry(client):
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    respx.get(f"{GREENHOUSE_API_URL}/12345").mock(side_effect=_handler)

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await client.fetch_job_detail(12345)
    assert exc.value.response.status_code == 404
    assert calls["n"] == 1  # NOT retried


@respx.mock
@pytest.mark.asyncio
async def test_500_surfaces_immediately_without_retry(client):
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="server error")

    respx.get(GREENHOUSE_API_URL).mock(side_effect=_handler)

    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_jobs()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# int(job_id) coercion / path-injection rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_job_detail_rejects_path_injection(client):
    # A non-numeric job_id must raise ValueError before any request is built,
    # so "../../secret" can never become part of the URL path.
    with pytest.raises(ValueError):
        await client.fetch_job_detail("../../admin")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await client.fetch_job_detail("1; DROP")  # type: ignore[arg-type]


@respx.mock
@pytest.mark.asyncio
async def test_fetch_job_detail_coerces_numeric_string(client):
    # A clean numeric string is coerced via int() and used as the path.
    route = respx.get(f"{GREENHOUSE_API_URL}/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "title": "Role"})
    )
    job = await client.fetch_job_detail("42")  # type: ignore[arg-type]
    assert job["id"] == 42
    assert route.called


# ---------------------------------------------------------------------------
# department mapping helpers
# ---------------------------------------------------------------------------


def test_build_department_map_and_enrich():
    departments = [
        {"id": 10, "name": "Research", "jobs": [{"id": 1}, {"id": 2}]},
        {"id": 20, "name": "Security", "jobs": [{"id": 3}]},
        {"id": 30, "name": "Empty", "jobs": None},
    ]
    dept_map = build_department_map(departments)
    assert dept_map[1]["name"] == "Research"
    assert dept_map[3]["name"] == "Security"
    assert 99 not in dept_map

    jobs = [{"id": 1}, {"id": 3}, {"id": 99}]
    enriched = enrich_jobs_with_departments(jobs, dept_map)
    assert enriched[0]["departments"][0]["name"] == "Research"
    assert enriched[1]["departments"][0]["name"] == "Security"
    # unknown job id gets no department attached
    assert "departments" not in enriched[2]
