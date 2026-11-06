from __future__ import annotations

import asyncio

import httpx
import pytest

from signaleyes_mcp.client import (
    RetryingClient,
    check_response,
    creates_task,
    error_outcome,
    parse_tagged_error,
    poll_until_done,
    recovery_hint,
    retry_delay,
    tag_error,
    task_status,
)


def _response(status_code=200, json=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.test/"),
    )


# --- retrying client ----------------------------------------------------------


def _client(statuses: list[int], body=None) -> tuple[RetryingClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = statuses[min(len(seen), len(statuses)) - 1]
        return httpx.Response(status, json=body or {"status": "x"})

    client = RetryingClient(
        base_url="https://example.test/v1", transport=httpx.MockTransport(handler)
    )
    return client, seen


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", instant)


def test_get_502_is_retried_then_succeeds():
    client, seen = _client([502, 502, 200])
    resp = asyncio.run(client.get("/subscriptions"))
    assert resp.status_code == 200
    assert len(seen) == 3


def test_get_500_is_not_retried_so_failed_task_bodies_are_read_once():
    client, seen = _client([500], {"id": "t", "status": "failed", "error": "boom"})
    resp = asyncio.run(client.get("/task/t"))
    assert resp.status_code == 500
    assert len(seen) == 1


def test_post_502_is_not_retried():
    client, seen = _client([502, 200])
    resp = asyncio.run(client.post("/source/tree", json="POLYGON"))
    assert resp.status_code == 502
    assert len(seen) == 1


def test_task_creating_get_502_is_not_retried():
    client, seen = _client([502, 200])
    resp = asyncio.run(client.get("/concept/buffer", params={"radius": 1}))
    assert resp.status_code == 502
    assert len(seen) == 1
    client, seen = _client([502, 200])
    resp = asyncio.run(client.get("/source/tree", headers={"Prefer": "respond-async"}))
    assert resp.status_code == 502
    assert len(seen) == 1


def test_429_is_retried_even_for_task_creating_requests():
    client, seen = _client([429, 200])
    resp = asyncio.run(client.get("/concept/buffer"))
    assert resp.status_code == 200
    assert len(seen) == 2


def test_connect_error_is_retried_for_safe_requests_only():
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={})

    client = RetryingClient(base_url="https://example.test", transport=httpx.MockTransport(flaky))
    assert asyncio.run(client.get("/task/x")).status_code == 200
    calls["n"] = 0
    with pytest.raises(httpx.ConnectError):
        asyncio.run(client.post("/source/geojson", json={}))


def test_creates_task_rules():
    assert not creates_task(httpx.Request("GET", "https://x/v1/task/abc"))
    assert creates_task(httpx.Request("GET", "https://x/v1/concept/overlay"))
    assert creates_task(httpx.Request("GET", "https://x/v1/analyses/baf"))
    assert creates_task(
        httpx.Request("GET", "https://x/v1/source/tree", headers={"Prefer": "respond-async"})
    )
    assert creates_task(httpx.Request("POST", "https://x/v1/source/tree"))


def test_retry_delay_stays_within_jitter_band():
    delays = [retry_delay(1, backoff=0.4) for _ in range(40)]
    # attempt 1, backoff 0.4 → 0.8 * [0.5, 1.5) = [0.4, 1.2)
    assert all(0.4 <= d < 1.2 for d in delays)
    assert min(delays) != max(delays)


# --- error text --------------------------------------------------------------


def test_check_response_passes_through_on_success():
    check_response(_response(200, json={"ok": True}))  # must not raise


def test_check_response_folds_detail_into_the_error():
    with pytest.raises(httpx.HTTPStatusError, match="polygon must be in EPSG:4326"):
        check_response(_response(422, json={"detail": "polygon must be in EPSG:4326"}))


def test_check_response_tolerates_non_object_and_non_json_bodies():
    for resp in (
        httpx.Response(502, json="Bad Gateway", request=httpx.Request("GET", "https://x")),
        httpx.Response(500, text="oops", request=httpx.Request("GET", "https://x")),
    ):
        with pytest.raises(httpx.HTTPStatusError, match=str(resp.status_code)):
            check_response(resp)


def test_recovery_hint_table():
    assert "wait_for_task" in (recovery_hint(409, True) or "")
    assert "Rate limited" in (recovery_hint(429, True) or "")
    assert "manage_task" in (recovery_hint(502, False) or "")
    assert "Retry" in (recovery_hint(502, True) or "")
    assert "whoami" in (recovery_hint(401, True) or "")
    assert recovery_hint(200, True) is None
    hint = recovery_hint(403, True, "User x has no active subscription for the requested area")
    assert hint and "subscription" in hint and "credentials" not in hint


@pytest.mark.parametrize(
    ("message", "retryable", "fragment"),
    [
        ("Client error '409 Conflict' for url 'https://x'", True, "wait_for_task"),
        ("HTTP error 409: Conflict - {'detail': 'not done'}", True, "wait_for_task"),
        ("HTTP error 429: Too Many Requests - {}", True, "Rate limited"),
        ("HTTP error 503: Service Unavailable - {}", True, "Retry"),
        ("Client error '401 Unauthorized' for url 'https://x'", False, "whoami"),
        ("Task t1 did not complete within 5s; it is still running", True, "wait_for_task"),
    ],
)
def test_error_outcome_reads_both_error_formats(message, retryable, fragment):
    got_retryable, hint = error_outcome(message, read_only=True)
    assert got_retryable is retryable
    assert hint and fragment in hint


def test_error_outcome_5xx_on_a_write_tool_is_not_retryable():
    retryable, hint = error_outcome("HTTP error 502: Bad Gateway - {}", read_only=False)
    assert retryable is False
    assert hint and "manage_task" in hint


def test_tag_error_round_trips_through_parse_tagged_error():
    tagged = tag_error("HTTP error 429: Too Many Requests - {}", read_only=True)
    assert tagged.endswith("[retryable=yes]")
    assert parse_tagged_error(tagged) == (True, "Rate limited. Wait, then retry.")
    assert parse_tagged_error("no tag here") == (False, None)
    assert tag_error("'nope' has no list endpoint", read_only=True) == "'nope' has no list endpoint"


# --- task status / polling ----------------------------------------------------


def test_task_status_reads_failed_body_behind_a_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"id": "t", "status": "failed", "error": "boom"})

    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    assert asyncio.run(task_status(client, "t"))["status"] == "failed"


def test_task_status_raises_for_a_500_without_a_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "Internal server error"})

    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError, match="Internal server error"):
        asyncio.run(task_status(client, "t"))


def test_poll_until_done_reports_failure_without_deleting():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(500, json={"id": "t", "status": "failed", "error": "boom"})

    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="boom.*delete_task"):
        asyncio.run(poll_until_done(client, "t", max_wait=5))
    assert seen == ["GET"]
