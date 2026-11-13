from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from fastmcp.exceptions import ToolError

from signaleyes_mcp import build_server


def _response(status_code: int = 200, json=None, headers=None) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        json=json,
        headers=headers or {},
        request=httpx2.Request("GET", "https://example.test/"),
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


def test_wait_for_task_reports_a_failed_task_and_does_not_delete_it(
    monkeypatch, mock_spec_get, spy_async_client, async_client_get_queue
):
    deleted = []

    async def fake_delete(self, url, **kwargs):
        deleted.append(url)
        return _response(status_code=204)

    monkeypatch.setattr(httpx2.AsyncClient, "delete", fake_delete)
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"id": "t1", "status": "failed", "error": "out of memory"})
    )

    with pytest.raises(ToolError, match="out of memory.*delete_task"):
        _call(server, "wait_for_task", {"task_id": "t1"})
    assert deleted == []


def test_wait_for_task_treats_500_with_failed_body_as_failed_not_transient(
    monkeypatch, mock_spec_get, spy_async_client, async_client_get_queue
):
    """The live API answers GET /task/{id} for a failed task with HTTP 500."""
    deleted = []

    async def fake_delete(self, url, **kwargs):
        deleted.append(url)
        return _response(status_code=200, json={"id": "t1", "deleted": True})

    monkeypatch.setattr(httpx2.AsyncClient, "delete", fake_delete)
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(status_code=500, json={"id": "t1", "status": "failed", "error": "ISE"})
    )

    with pytest.raises(ToolError, match="failed: ISE.*delete_task"):
        _call(server, "wait_for_task", {"task_id": "t1"})
    assert len(async_client_get_queue.calls) == 1
    assert deleted == []


def test_wait_for_task_raises_timeout_when_never_done(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [_response(json={"id": "t1", "status": "running", "error": None}) for _ in range(4)]
    )

    with pytest.raises(ToolError, match="did not complete"):
        _call(server, "wait_for_task", {"task_id": "t1", "max_wait": 4, "poll_interval": 2})


def test_wait_for_task_retries_past_a_transient_timeout(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    """A slow poll doesn't mean the task failed — the field case this guards against:
    the task was already 'done' upstream, but the /task/{id} poll itself timed out,
    and the old behavior surfaced that as a hard failure instead of retrying."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            httpx2.TimeoutException("timed out"),
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(json={"type": "FeatureCollection", "features": [{"id": 1}]}),
        ]
    )

    result = _call(server, "wait_for_task", {"task_id": "t1"})

    assert result.structured_content["features"] == [{"id": 1}]
    assert len(async_client_get_queue.calls) == 3


def test_wait_for_task_retries_a_timeout_on_the_result_fetch_too(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"id": "t1", "status": "done", "error": None}),
            httpx2.TimeoutException("timed out"),
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(json={"type": "FeatureCollection", "features": [{"id": 1}]}),
        ]
    )

    result = _call(server, "wait_for_task", {"task_id": "t1"})

    assert result.structured_content["features"] == [{"id": 1}]


def test_wait_for_task_surfaces_a_503_the_client_could_not_retry_away(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep
):
    """HTTP-level retries live in the client; the poller doesn't add a second layer."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(status_code=503, json={"detail": "down"}))

    with pytest.raises(ToolError, match=r"503.*down.*\[retryable=yes\]"):
        _call(server, "wait_for_task", {"task_id": "t1"})


def test_wait_for_task_does_not_retry_a_422(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(status_code=422, json={"detail": "bad task id"})
    )

    with pytest.raises(ToolError, match="bad task id"):
        _call(server, "wait_for_task", {"task_id": "t1"})


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


def test_fetch_all_pages_omits_unset_optional_params(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"features": []}, headers={"X-Total-Pages": "1"})
    )

    _call(server, "fetch_all_pages", {"entity": "building", "polygon": "POLYGON ((0 0))"})

    assert async_client_get_queue.calls[0]["params"] == {"polygon": "POLYGON ((0 0))", "page": 1}


def test_fetch_all_pages_passes_through_filters(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"features": []}, headers={"X-Total-Pages": "1"})
    )

    _call(
        server,
        "fetch_all_pages",
        {
            "entity": "building",
            "polygon": "POLYGON ((0 0))",
            "filter_string": "height=10",
            "geo_type": "centroid",
            "simplify": 0.5,
            "sample": 0.2,
            "dissolve": False,
            "intersect_centroid": False,
        },
    )

    assert async_client_get_queue.calls[0]["params"] == {
        "polygon": "POLYGON ((0 0))",
        "page": 1,
        "filter_string": "height=10",
        "geo_type": "centroid",
        "simplify": 0.5,
        "sample": 0.2,
        "dissolve": False,
        "intersect_centroid": False,
    }


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


def test_fetch_all_pages_unknown_entity_raises(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    with pytest.raises(ToolError, match="no list endpoint"):
        _call(server, "fetch_all_pages", {"entity": "nope", "polygon": "POLYGON ((0 0))"})


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


def test_fetch_all_pages_default_max_pages_caps_at_ten(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    """Defaults low (not "all") so an unexpectedly large area doesn't flood context."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"features": [{"id": p}]}, headers={"X-Total-Pages": "50"})
            for p in range(1, 11)
        ]
    )

    result = _call(server, "fetch_all_pages", {"entity": "building", "polygon": "POLYGON ((0 0))"})

    assert result.structured_content["pages_fetched"] == 10
    assert len(async_client_get_queue.calls) == 10


def test_fetch_all_pages_passes_through_attributes_and_groupby(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"features": []}, headers={"X-Total-Pages": "1"})
    )

    _call(
        server,
        "fetch_all_pages",
        {
            "entity": "thirdparty",
            "variant": "rdw",
            "polygon": "POLYGON ((0 0))",
            "attributes": "name,height",
            "groupby": "name",
        },
    )

    assert async_client_get_queue.calls[0]["params"] == {
        "polygon": "POLYGON ((0 0))",
        "page": 1,
        "attributes": "name,height",
        "groupby": "name",
    }


def test_fetch_all_pages_respond_async_returns_task_without_paginating(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"id": "t1", "status": "running"}))

    result = _call(
        server,
        "fetch_all_pages",
        {"entity": "building", "polygon": "POLYGON ((0 0))", "respond_async": True},
    )

    assert result.structured_content == {"id": "t1", "status": "running"}
    assert len(async_client_get_queue.calls) == 1
    assert async_client_get_queue.calls[0]["headers"] == {"Prefer": "respond-async"}
    assert async_client_get_queue.calls[0]["params"] == {"polygon": "POLYGON ((0 0))"}


# --- post_large_polygon --------------------------------------------------


def test_post_large_polygon_sends_polygon_as_json_body(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    """The bug this tool exists to work around: the API wants the polygon
    JSON-encoded as the request body, not appended as a query param."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(server, "post_large_polygon", {"entity": "bag", "polygon": "POLYGON ((0 0))"})

    call = async_client_post_queue.calls[0]
    assert call["url"] == "/source/bag"
    assert call["json"] == "POLYGON ((0 0))"
    assert call["params"] == {"page": 1}


def test_post_large_polygon_surfaces_pagination_headers(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    """The API paginates POST results the same way it paginates GET results —
    the caller needs total_pages/page to know whether to fetch page=2, etc."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(
            json={"type": "FeatureCollection", "features": []},
            headers={"X-Total-Pages": "3", "X-Page-Number": "1"},
        )
    )

    result = _call(server, "post_large_polygon", {"entity": "bag", "polygon": "POLYGON ((0 0))"})

    assert result.structured_content["total_pages"] == 3
    assert result.structured_content["page"] == 1


def test_post_large_polygon_omits_pagination_keys_when_headers_absent(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    """respond_async=True gets back a task-id body with no pagination headers."""
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(_response(json={"id": "t1", "status": "running"}))

    result = _call(
        server,
        "post_large_polygon",
        {"entity": "bag", "polygon": "POLYGON ((0 0))", "respond_async": True},
    )

    assert "total_pages" not in result.structured_content
    assert "page" not in result.structured_content


def test_post_large_polygon_uses_variant_path(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(
        server,
        "post_large_polygon",
        {"entity": "tree", "variant": "crown", "polygon": "POLYGON ((0 0))"},
    )

    assert async_client_post_queue.calls[0]["url"] == "/source/tree/crown"


def test_post_large_polygon_sets_respond_async_header(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(_response(json={"id": "t1", "status": "running"}))

    _call(
        server,
        "post_large_polygon",
        {"entity": "bag", "polygon": "POLYGON ((0 0))", "respond_async": True},
    )

    assert async_client_post_queue.calls[0]["headers"] == {"Prefer": "respond-async"}


def test_post_large_polygon_omits_unset_optional_params(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(server, "post_large_polygon", {"entity": "bag", "polygon": "POLYGON ((0 0))"})

    assert async_client_post_queue.calls[0]["params"] == {"page": 1}
    assert async_client_post_queue.calls[0]["headers"] == {}


def test_post_large_polygon_passes_through_filters(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(
        server,
        "post_large_polygon",
        {
            "entity": "bag",
            "polygon": "POLYGON ((0 0))",
            "filter_string": "height=10",
            "geo_type": "centroid",
            "simplify": 0.5,
            "sample": 0.2,
            "dissolve": False,
            "intersect_centroid": False,
        },
    )

    assert async_client_post_queue.calls[0]["params"] == {
        "page": 1,
        "filter_string": "height=10",
        "geo_type": "centroid",
        "simplify": 0.5,
        "sample": 0.2,
        "dissolve": False,
        "intersect_centroid": False,
    }


# --- wait_for_tasks --------------------------------------------------------


def test_wait_for_tasks_describes_each_and_isolates_failures(
    monkeypatch, mock_spec_get, spy_async_client
):
    """Responses keyed by URL (not a queue) because the waits run concurrently."""
    by_url = {
        "/task/a": _response(json={"id": "a", "status": "done", "error": None}),
        "/task/a/describe": _response(json={"tree": {"number_of_features": 3}}),
        "/task/b": _response(json={"id": "b", "status": "failed", "error": "boom"}),
    }

    async def fake_get(self, url, **kwargs):
        return by_url[url]

    async def fake_delete(self, url, **kwargs):
        return _response(status_code=200)

    monkeypatch.setattr(httpx2.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx2.AsyncClient, "delete", fake_delete)
    server = _server(mock_spec_get, spy_async_client)

    out = _call(server, "wait_for_tasks", {"task_ids": ["a", "b"]}).structured_content

    assert out["a"] == {"tree": {"number_of_features": 3}}
    assert "boom" in out["b"]["error"]


def test_wait_for_tasks_rejects_unknown_view(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)
    with pytest.raises(ToolError, match="describe"):
        _call(server, "wait_for_tasks", {"task_ids": ["a"], "view": "status"})


# --- size cap -------------------------------------------------------------


def _big_feature(i: int) -> dict:
    return {"id": i, "geometry": {"coordinates": [[0.123456789] * 200]}}


def test_wait_for_task_trims_whole_features_under_the_cap(
    monkeypatch, mock_spec_get, spy_async_client, async_client_get_queue
):
    monkeypatch.setenv("SIGNALEYES_MAX_TOOL_RESPONSE_BYTES", "5000")
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(
                json={"type": "FeatureCollection", "features": [_big_feature(i) for i in range(50)]}
            ),
        ]
    )

    result = _call(server, "wait_for_task", {"task_id": "t1"})

    body = result.structured_content
    assert body["truncated"] is True
    assert body["total_features"] == 50
    assert 0 < len(body["features"]) < 50
    assert body["features"][0]["id"] == 0
    assert len(json.dumps(body)) <= 5000


def test_fetch_all_pages_stops_paging_once_past_the_cap(
    monkeypatch, mock_spec_get, spy_async_client, async_client_get_queue
):
    monkeypatch.setenv("SIGNALEYES_MAX_TOOL_RESPONSE_BYTES", "3000")
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [
            _response(json={"features": [_big_feature(p)]}, headers={"X-Total-Pages": "10"})
            for p in range(1, 11)
        ]
    )

    result = _call(server, "fetch_all_pages", {"entity": "building", "polygon": "POLYGON ((0 0))"})

    assert len(async_client_get_queue.calls) == 2
    assert result.structured_content["pages_fetched"] == 2
    assert result.structured_content["total_pages"] == 10


def test_wait_for_task_polls_fast_first_then_backs_off(
    mock_spec_get, spy_async_client, async_client_get_queue, monkeypatch
):
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [_response(json={"id": "t1", "status": "running", "error": None}) for _ in range(5)]
        + [
            _response(json={"id": "t1", "status": "done", "error": None}),
            _response(json={"type": "FeatureCollection", "features": []}),
        ]
    )

    _call(server, "wait_for_task", {"task_id": "t1"})

    assert sleeps == [0.3, 0.6, 1.2, 2.0, 2.0]


# --- whoami -------------------------------------------------------------


def test_whoami_returns_subscriptions_body(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"plan": "pro", "seats": 3}))

    result = _call(server, "whoami", {})

    assert result.structured_content == {"plan": "pro", "seats": 3}
    assert async_client_get_queue.calls[0]["url"] == "/subscriptions"


def test_whoami_replaces_aoi_wkt_with_bbox(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    wkt = "POLYGON ((5.0 52.0, 5.0 52.5, 5.25 52.5, 5.25 52.0, 5.0 52.0))"
    async_client_get_queue.responses.append(
        _response(json={"username": "u", "subscriptions": [{"aoi": wkt, "end_date": "2027"}]})
    )

    result = _call(server, "whoami", {})

    assert result.structured_content["subscriptions"][0] == {
        "aoi": {"bbox": [5.0, 52.0, 5.25, 52.5], "wkt_chars": len(wkt)},
        "end_date": "2027",
    }


def test_whoami_propagates_http_error(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(status_code=401, json={"detail": "unauthorized"})
    )

    with pytest.raises(ToolError):
        _call(server, "whoami", {})


# --- get_object_by_id -----------------------------------------------------


def test_get_object_by_id_no_variant(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"id": "42", "type": "Feature"}))

    result = _call(server, "get_object_by_id", {"entity": "agriparcel", "object_id": "42"})

    assert result.structured_content == {"id": "42", "type": "Feature"}
    assert async_client_get_queue.calls[0]["url"] == "/source/agriparcel/42"


def test_get_object_by_id_with_variant(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"id": "42", "type": "Feature"}))

    _call(
        server,
        "get_object_by_id",
        {"entity": "building", "object_id": "42", "variant": "school"},
    )

    assert async_client_get_queue.calls[0]["url"] == "/source/building/school/42"


def test_get_object_by_id_missing_required_variant_raises(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    with pytest.raises(ToolError, match="requires a variant"):
        _call(server, "get_object_by_id", {"entity": "building", "object_id": "42"})


def test_get_object_by_id_unsupported_variant_raises(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    with pytest.raises(ToolError, match="doesn't take a variant"):
        _call(
            server,
            "get_object_by_id",
            {"entity": "agriparcel", "object_id": "42", "variant": "x"},
        )


def test_get_object_by_id_unknown_entity_raises(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    with pytest.raises(ToolError, match="no by_id endpoint"):
        _call(server, "get_object_by_id", {"entity": "nope", "object_id": "1"})


# --- post_objects_by_ids ---------------------------------------------------


def test_post_objects_by_ids_sends_ids_body(
    mock_spec_get, spy_async_client, async_client_post_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(server, "post_objects_by_ids", {"entity": "agriparcel", "ids": ["1", "2"]})

    call = async_client_post_queue.calls[0]
    assert call["url"] == "/source/agriparcel/batch"
    assert call["json"] == {"ids": ["1", "2"]}


def test_post_objects_by_ids_with_variant(mock_spec_get, spy_async_client, async_client_post_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_post_queue.responses.append(
        _response(json={"type": "FeatureCollection", "features": []})
    )

    _call(
        server,
        "post_objects_by_ids",
        {"entity": "building", "ids": ["1"], "variant": "school"},
    )

    assert async_client_post_queue.calls[0]["url"] == "/source/building/school/batch"


# --- get_objects_metadata ---------------------------------------------------


def test_get_objects_metadata_no_variant(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"fields": ["height"]}))

    result = _call(server, "get_objects_metadata", {"entity": "agriparcel"})

    assert result.structured_content == {"fields": ["height"]}
    assert async_client_get_queue.calls[0]["url"] == "/source/agriparcel/metadata"


def test_get_objects_metadata_with_variant(mock_spec_get, spy_async_client, async_client_get_queue):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"fields": []}))

    _call(server, "get_objects_metadata", {"entity": "building", "variant": "school"})

    assert async_client_get_queue.calls[0]["url"] == "/source/building/school/metadata"


# --- list_source_entities ---------------------------------------------------


def test_list_source_entities_describes_support_matrix(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    result = _call(server, "list_source_entities", {})

    described = result.structured_content
    assert described["agriparcel"] == {
        "list": "no variant",
        "by_id": "no variant",
        "metadata": "no variant",
        "batch": "no variant",
    }
    assert described["building"]["list"] == "variant optional"
    assert described["building"]["by_id"] == "variant required"
    assert described["building_inactive"]["list"] == "variant required"
    assert "by_id" not in described["dsm_pages"]


# --- manage_task -------------------------------------------------------


@pytest.mark.parametrize(
    ("view", "url"),
    [
        ("status", "/task/t1"),
        ("describe", "/task/t1/describe"),
        ("metadata", "/task/t1/metadata"),
        ("result", "/task/t1/result"),
    ],
)
def test_manage_task_views_hit_the_right_url(
    mock_spec_get, spy_async_client, async_client_get_queue, view, url
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"ok": True}))

    result = _call(server, "manage_task", {"task_id": "t1", "view": view})

    assert result.structured_content == {"ok": True}
    assert async_client_get_queue.calls[0]["url"] == url


def test_manage_task_defaults_to_status_view(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(_response(json={"status": "running"}))

    _call(server, "manage_task", {"task_id": "t1"})

    assert async_client_get_queue.calls[0]["url"] == "/task/t1"


def test_manage_task_rejects_unknown_view(mock_spec_get, spy_async_client):
    server = _server(mock_spec_get, spy_async_client)

    with pytest.raises(ToolError):
        _call(server, "manage_task", {"task_id": "t1", "view": "delete"})


def test_manage_task_status_returns_a_failed_task_instead_of_raising(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(status_code=500, json={"id": "t1", "status": "failed", "error": "boom"})
    )

    out = _call(server, "manage_task", {"task_id": "t1"}).structured_content
    assert out == {"id": "t1", "status": "failed", "error": "boom"}


def test_fetch_all_pages_marks_an_early_stop_as_truncated_without_a_fake_total(
    monkeypatch, mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.extend(
        [_response(json={"features": [{"id": p}]}, headers={"X-Total-Pages": "5"}) for p in (1, 2)]
    )

    out = _call(
        server,
        "fetch_all_pages",
        {"entity": "building", "polygon": "POLYGON ((0 0))", "max_pages": 2},
    ).structured_content

    assert out["truncated"] is True
    assert "total_features" not in out
    assert "describe" in out["hint"]


def test_whoami_leaves_3d_wkt_without_a_bbox(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    server = _server(mock_spec_get, spy_async_client)
    async_client_get_queue.responses.append(
        _response(json={"subscriptions": [{"aoi": "POLYGON Z ((1 2 3, 4 5 6, 1 2 3))"}]})
    )

    out = _call(server, "whoami", {}).structured_content
    assert out["subscriptions"][0]["aoi"] == {"wkt_chars": len("POLYGON Z ((1 2 3, 4 5 6, 1 2 3))")}
