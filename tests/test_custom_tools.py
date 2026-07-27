from __future__ import annotations

import asyncio

import httpx
import pytest
from fastmcp.exceptions import ToolError

from signaleyes_mcp import build_server


def _response(status_code: int = 200, json=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json,
        headers=headers or {},
        request=httpx.Request("GET", "https://example.test/"),
    )


def _server(mock_spec_get, spy_async_client):
    return build_server("https://example.test/v1", "user", "pass")


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


# --- wait_for_task ----------------------------------------------------------


def test_wait_for_task_returns_result_when_already_done(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(json={"type": "FeatureCollection", "features": [{"id": 1}]}),
        ]
    )

    result = _call(server, "wait_for_task", {"task_id": "t1"})

    assert result.structured_content["features"] == [{"id": 1}]
    assert async_client_get_queue.calls[0]["url"] == "/task/t1"
    assert async_client_get_queue.calls[1]["url"] == "/task/t1/result"


def test_wait_for_task_polls_until_done(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"id": "t1", "status": "queued", "error": None}),
            _response(json={"id": "t1", "status": "running", "error": None}),
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(json={"type": "FeatureCollection", "features": []}),
        ]
    )

    result = _call(server, "wait_for_task", {"task_id": "t1"})

    assert result.structured_content == {"type": "FeatureCollection", "features": []}
    assert len(async_client_get_queue.calls) == 4


def test_wait_for_task_raises_on_failed_status(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"id": "t1", "status": "failed", "error": "bad polygon"})
    )

    with pytest.raises(ToolError, match="bad polygon"):
        _call(server, "wait_for_task", {"task_id": "t1"})


def test_wait_for_task_raises_timeout_when_never_done(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [_response(json={"id": "t1", "status": "running", "error": None}) for _ in range(2)]
    )

    with pytest.raises(ToolError, match="did not complete"):
        _call(server, "wait_for_task", {"task_id": "t1", "timeout": 4, "poll_interval": 2})


# --- fetch_all_pages ---------------------------------------------------------


def test_fetch_all_pages_merges_features_across_pages(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"features": [{"id": p}]}, headers={"X-Total-Pages": "3"})
            for p in range(1, 4)
        ]
    )

    result = _call(server, "fetch_all_pages", {"entity": "building", "polygon": "POLYGON ((0 0))"})

    assert result.structured_content["pages_fetched"] == 3
    assert result.structured_content["total_pages"] == 3
    assert [f["id"] for f in result.structured_content["features"]] == [1, 2, 3]


def test_fetch_all_pages_uses_variant_path(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"features": []}, headers={"X-Total-Pages": "1"})
    )

    _call(
        server,
        "fetch_all_pages",
        {"entity": "tree", "polygon": "POLYGON ((0 0))", "variant": "crown"},
    )

    assert async_client_get_queue.calls[0]["url"] == "/source/tree/crown"


def test_fetch_all_pages_caps_at_max_pages(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"features": [{"id": p}]}, headers={"X-Total-Pages": "5"})
            for p in range(1, 3)
        ]
    )

    result = _call(
        server,
        "fetch_all_pages",
        {"entity": "building", "polygon": "POLYGON ((0 0))", "max_pages": 2},
    )

    assert result.structured_content["pages_fetched"] == 2
    assert result.structured_content["total_pages"] == 5
    assert len(async_client_get_queue.calls) == 2


# --- whoami -------------------------------------------------------------


def test_whoami_returns_subscriptions_body(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"plan": "pro", "seats": 3}))

    result = _call(server, "whoami", {})

    assert result.structured_content == {"plan": "pro", "seats": 3}
    assert async_client_get_queue.calls[0]["url"] == "/subscriptions"


def test_whoami_propagates_http_error(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(status_code=401, json={"detail": "unauthorized"})
    )

    with pytest.raises(ToolError):
        _call(server, "whoami", {})
