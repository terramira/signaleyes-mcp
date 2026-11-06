"""HTTP layer for the SignalEyes API: retrying client, error text, task polling.

One table decides what an error means (`recovery_hint`) and one place
attaches it to every tool error (`error_outcome`, used by the server's error
middleware), so the orchestrator client and the skill don't carry their own
copies.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any

import httpx

# 500 is deliberately absent: the API answers GET /task/{id} for a *failed*
# task with HTTP 500 and {"status": "failed"} in the body, and retrying that
# three times only delays the poller reading it.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
TASK_CREATING_PATH = re.compile(r"/(concept|analyses)/")

# Both error formats this server produces: httpx's "Client error '409 Conflict'
# for url ..." (hand-written tools) and FastMCP's "HTTP error 409: Conflict -
# {...}" (generated tools).
_STATUS_RE = re.compile(r"(?:'|HTTP error )([45]\d{2})(?:\s|:)")
_OUTCOME_RE = re.compile(r" — (?P<hint>[^—]*) \[retryable=(?P<retryable>yes|no)\]$")


def creates_task(request: httpx.Request) -> bool:
    """A request that may enqueue a server-side task must never be re-sent on 5xx."""
    if request.method != "GET":
        return True
    if TASK_CREATING_PATH.search(request.url.path):
        return True
    return "respond-async" in request.headers.get("Prefer", "")


def retry_delay(attempt: int, backoff: float = 0.4) -> float:
    """Exponential backoff with jitter so concurrent agents don't retry in lockstep."""
    return float(backoff * (2**attempt) * (0.5 + random.random()))


class RetryingClient(httpx.AsyncClient):
    """httpx.AsyncClient that re-sends on connect errors, 429, and gateway 5xx.

    Retries live in `send()` rather than a custom transport so httpx keeps
    its default transport — and with it HTTP(S)_PROXY / NO_PROXY support,
    which a custom transport silently disables.
    """

    retries = 3

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = await super().send(request, **kwargs)
            except httpx.ConnectError:
                if attempt == self.retries or creates_task(request):
                    raise
                await asyncio.sleep(retry_delay(attempt))
                continue
            status = response.status_code
            retry = status == 429 or (status in RETRYABLE_STATUSES and not creates_task(request))
            if attempt == self.retries or not retry:
                return response
            await response.aclose()
            await asyncio.sleep(retry_delay(attempt))
        raise AssertionError("unreachable")


def make_client(base_url: str, username: str, password: str) -> httpx.AsyncClient:
    # httpx's default 5s timeout is too tight for this API: task-status
    # polls, /result and /describe on large datasets have all been observed
    # taking longer under normal conditions. 10s to connect, 60s to read.
    return RetryingClient(
        base_url=base_url,
        auth=httpx.BasicAuth(username, password),
        timeout=httpx.Timeout(10.0, read=60.0),
    )


def recovery_hint(status: int | None, read_only: bool, detail: str = "") -> str | None:
    """What the caller should do about an error. The single source of truth."""
    if status == 403 and "subscription" in detail:
        return (
            "Area or source is outside this account's subscription. whoami shows the "
            "subscribed bbox; do not retry the same polygon."
        )
    if status in (401, 403):
        return "Call whoami once; do not retry other tools until credentials work."
    if status == 422:
        return "Fix the arguments; do not retry the same call."
    if status == 429:
        return "Rate limited. Wait, then retry."
    if status == 409:
        return "Task is not done yet. Use wait_for_task."
    if status in (502, 503, 504):
        if read_only:
            return "Transient upstream error. Retry."
        return "Do not retry this call (a task may already exist). Check manage_task."
    lower = detail.lower()
    if "did not complete within" in lower:
        return "Task still running. Call wait_for_task again, with a larger max_wait."
    if "timed out" in lower or "timeout" in lower:
        return "Transient timeout. Retry."
    return None


def error_outcome(message: str, read_only: bool) -> tuple[bool, str | None]:
    """(retryable, hint) for a tool error message, in either error format."""
    match = _STATUS_RE.search(message)
    status = int(match.group(1)) if match else None
    hint = recovery_hint(status, read_only, message)
    retryable = hint is not None and hint.startswith(("Rate limited", "Task", "Transient"))
    return retryable, hint


def tag_error(message: str, read_only: bool) -> str:
    """Append the hint and a machine-readable retryable marker to a tool error."""
    retryable, hint = error_outcome(message, read_only)
    if hint is None:
        return message
    return f"{message} — {hint} [retryable={'yes' if retryable else 'no'}]"


def parse_tagged_error(message: str) -> tuple[bool, str | None]:
    """Inverse of `tag_error` for clients: (retryable, hint), or (False, None) if untagged."""
    match = _OUTCOME_RE.search(message)
    if not match:
        return False, None
    return match.group("retryable") == "yes", match.group("hint")


def json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def check_response(resp: httpx.Response) -> None:
    """Like resp.raise_for_status(), but folds the API's JSON `detail` into the error.

    The API returns structured 4xx/5xx bodies (e.g. 422 validation errors)
    that raise_for_status() alone discards.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = json_body(resp).get("detail")
        if not detail:
            raise
        raise httpx.HTTPStatusError(
            f"{exc} — {detail}", request=exc.request, response=exc.response
        ) from exc


class TaskFailed(RuntimeError):
    pass


async def task_status(client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    """GET /task/{id} as the API means it, not as its status code says.

    A failed task comes back as HTTP 500 with {"status": "failed", ...} in the
    body; the body wins whenever it carries a status.
    """
    resp = await client.get(f"/task/{task_id}")
    body = json_body(resp)
    if "status" not in body:
        check_response(resp)
    return body


async def poll_until_done(
    client: httpx.AsyncClient,
    task_id: str,
    max_wait: float,
    poll_interval: float = 2.0,
    fetch_result: bool = False,
) -> dict[str, Any] | None:
    """Block until a task is done (optionally returning its result); raise on failure or deadline.

    Polling starts at 0.3s and doubles up to `poll_interval`: small tasks
    finish in a few seconds, so a fixed 2s tick wasted up to 2s per wait. A
    transient timeout on one poll is retried on the next tick; HTTP-level
    retries are the client's job. A failed task is reported, not deleted:
    the wait tools are read-only, and `delete_task` exists for cleanup.
    """
    delay = min(0.3, poll_interval)
    try:
        async with asyncio.timeout(max_wait):
            while True:
                try:
                    body = await task_status(client, task_id)
                    status = body.get("status")
                    if status == "done":
                        if not fetch_result:
                            return None
                        result_resp = await client.get(f"/task/{task_id}/result")
                        check_response(result_resp)
                        result: dict[str, Any] = result_resp.json()
                        return result
                    if status == "failed":
                        raise TaskFailed(
                            f"Task {task_id} failed: {body.get('error')}. "
                            f"delete_task('{task_id}') to clean it up."
                        )
                except (httpx.TimeoutException, httpx.NetworkError):
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, poll_interval)
    except TimeoutError as exc:
        raise TimeoutError(
            f"Task {task_id} did not complete within {max_wait}s; it is still running "
            "server-side and cannot be deleted until it finishes."
        ) from exc
