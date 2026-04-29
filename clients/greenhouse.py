"""Async Greenhouse client for Anthropic's public job board.

Hits the public boards-api endpoint (no auth) and returns parsed JSON. Uses
httpx with exponential-backoff retries that mirror the upstream tracker's
fetcher.py (MAX_RETRIES = 2, RETRY_BACKOFF = 1.0 doubled per attempt: 1s, 2s).

Raises httpx.HTTPStatusError on non-2xx responses (including 404), and
httpx.TransportError / httpx.TimeoutException on network failures. Tools that
call into this client are responsible for catching and shaping errors.
"""

import asyncio

import httpx

GREENHOUSE_API_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
GREENHOUSE_DEPARTMENTS_URL = (
    "https://boards-api.greenhouse.io/v1/boards/anthropic/departments"
)
USER_AGENT = "anthropic-tracker-mcp/0.1.0"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0  # seconds, doubled each retry → 1s, 2s


class GreenhouseClient:
    """Thin async wrapper around the Greenhouse public job-board API.

    A single instance is created at module load in `server.py` so the
    underlying httpx connection pool is reused across tool calls.
    """

    def __init__(
        self,
        base_url: str = GREENHOUSE_API_URL,
        user_agent: str = USER_AGENT,
        timeout: float = REQUEST_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request_with_retry(self, url: str) -> dict:
        """GET with exponential backoff. Re-raises after MAX_RETRIES.

        Retries on httpx.TransportError (network) only. HTTPStatusError
        (4xx/5xx) is raised on the first failure so callers can distinguish
        404 from "API unavailable".
        """
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError:
                # Don't retry on 4xx/5xx — surface immediately.
                raise
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF * (2**attempt))
        # Exhausted retries
        assert last_exc is not None  # for type-checkers
        raise last_exc

    async def fetch_jobs(self) -> list[dict]:
        """Fetch all open Anthropic jobs from the Greenhouse board API.

        Returns a list of job dicts as returned by the API (id, title,
        absolute_url, location, first_published, etc.).

        Note: the bare `/jobs` endpoint does NOT include department data.
        Callers that need departments should also call `fetch_departments()`
        and merge with `enrich_jobs_with_departments()`.
        """
        data = await self._request_with_retry(self.base_url)
        return data.get("jobs", [])

    async def fetch_departments(self) -> list[dict]:
        """Fetch all departments. Each department nests its job IDs.

        Used to build a job_id -> department map since `/jobs` itself
        doesn't include department data.
        """
        data = await self._request_with_retry(GREENHOUSE_DEPARTMENTS_URL)
        return data.get("departments", [])

    async def fetch_job_detail(self, job_id: int) -> dict:
        """Fetch a single job with full HTML content for salary parsing.

        Raises httpx.HTTPStatusError(404) if the job_id doesn't exist on
        Anthropic's board.
        """
        url = f"{self.base_url}/{int(job_id)}"
        return await self._request_with_retry(url)


def build_department_map(departments: list[dict]) -> dict[int, dict]:
    """Map job_id -> department info from the /departments response.

    The Greenhouse departments endpoint nests jobs inside each department.
    """
    job_to_dept: dict[int, dict] = {}
    for dept in departments:
        dept_info = {"id": dept.get("id"), "name": dept.get("name")}
        for job in dept.get("jobs") or []:
            jid = job.get("id")
            if jid is not None:
                job_to_dept[jid] = dept_info
    return job_to_dept


def enrich_jobs_with_departments(
    jobs: list[dict], dept_map: dict[int, dict]
) -> list[dict]:
    """Attach department data to each job dict (in place) when missing."""
    for job in jobs:
        if not job.get("departments"):
            dept = dept_map.get(job.get("id"))
            if dept:
                job["departments"] = [
                    {
                        "id": dept["id"],
                        "name": dept["name"],
                        "child_ids": [],
                        "parent_id": None,
                    }
                ]
    return jobs
