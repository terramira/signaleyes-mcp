from __future__ import annotations

import asyncio

import httpx
import pytest

from signaleyes_mcp import build_server


def _tool_names(server) -> list[str]:
    return [t.name for t in asyncio.run(server.list_tools())]


def test_fetches_spec_from_base_url_slash_openapi_json(mock_spec_get, spy_async_client):
    build_server("https://example.test/v1", "user", "pass")
    assert mock_spec_get == ["https://example.test/v1/openapi.json"]


def test_wfs_endpoints_excluded(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert "wfs_tree" not in _tool_names(server)


def test_tile_endpoints_excluded(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert "tiles" not in _tool_names(server)


@pytest.mark.parametrize("name", ["dsm_quadkey", "dtm_quadkey"])
def test_raster_quadkey_endpoints_excluded(mock_spec_get, spy_async_client, name):
    server = build_server("https://example.test/v1", "user", "pass")
    assert name not in _tool_names(server)


@pytest.mark.parametrize("name", ["dsm_pages", "shadow_modelling", "usage_records"])
def test_binary_response_endpoints_excluded(mock_spec_get, spy_async_client, name):
    """These routes return raw image/tiff or xlsx bytes despite claiming application/json
    in the spec (confirmed against the live API) — a JSON tool result can't carry them."""
    server = build_server("https://example.test/v1", "user", "pass")
    assert name not in _tool_names(server)


def test_dsm_pages_metadata_excluded_in_favor_of_get_objects_metadata(
    mock_spec_get, spy_async_client
):
    """Not excluded by the binary-response pattern (it's real JSON) — excluded because
    every /source/*/metadata route, including this one, folds into get_objects_metadata."""
    server = build_server("https://example.test/v1", "user", "pass")
    assert "dsm_pages_metadata" not in _tool_names(server)
    assert "get_objects_metadata" in _tool_names(server)


def test_subscriptions_excluded_in_favor_of_whoami(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert "get_subscriptions" not in names
    assert "whoami" in names


@pytest.mark.parametrize("name", ["token_generate", "token_refresh", "token_revoke"])
def test_token_endpoints_excluded(mock_spec_get, spy_async_client, name):
    """These issue OAuth2 bearer tokens the server's fixed Basic-auth client never uses."""
    server = build_server("https://example.test/v1", "user", "pass")
    assert name not in _tool_names(server)


@pytest.mark.parametrize(
    "name",
    [
        "get_building",  # bare list GET
        "get_building_entity",  # variant list GET
        "get_agriparcel",  # bare list GET, no variant scheme at all
    ],
)
def test_single_page_list_get_excluded_in_favor_of_fetch_all_pages(
    mock_spec_get, spy_async_client, name
):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert name not in names
    assert "fetch_all_pages" in names


@pytest.mark.parametrize(
    "name",
    [
        "get_agriparcel_by_id",
        "get_building_by_id",
        "get_building_inactive_by_id",
    ],
)
def test_by_id_endpoints_excluded_in_favor_of_get_object_by_id(
    mock_spec_get, spy_async_client, name
):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert name not in names
    assert "get_object_by_id" in names


@pytest.mark.parametrize(
    "name",
    [
        "get_agriparcel_metadata",
        "get_building_entity_metadata",
        "get_building_inactive_metadata",
    ],
)
def test_metadata_endpoints_excluded_in_favor_of_get_objects_metadata(
    mock_spec_get, spy_async_client, name
):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert name not in names
    assert "get_objects_metadata" in names


@pytest.mark.parametrize(
    "name",
    [
        "post_agriparcel_batch",
        "post_building_batch",
        "post_building_inactive_batch",
    ],
)
def test_batch_endpoints_excluded_in_favor_of_post_objects_by_ids(
    mock_spec_get, spy_async_client, name
):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert name not in names
    assert "post_objects_by_ids" in names


def test_list_source_entities_tool_present(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert "list_source_entities" in _tool_names(server)


def test_bare_string_body_source_post_replaced_by_post_large_polygon(
    mock_spec_get, spy_async_client
):
    """POST /source/building's body is a bare WKT string, which FastMCP's OpenAPI client
    sends unserialized — the auto-generated tool always 400s upstream, so it's excluded
    in favor of the hand-written post_large_polygon tool."""
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert "post_building" not in names
    assert "post_large_polygon" in names


def test_task_read_endpoints_excluded_in_favor_of_manage_task(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    for excluded in ("get_task", "describe_task", "task_metadata", "task_result"):
        assert excluded not in names
    assert "manage_task" in names


def test_delete_task_stays_its_own_tool(mock_spec_get, spy_async_client):
    """manage_task only covers reads — deletion needs its own tool so an MCP client
    that allowlists reads on manage_task doesn't thereby approve deleting a task."""
    server = build_server("https://example.test/v1", "user", "pass")
    assert "delete_task" in _tool_names(server)


def test_no_duplicate_tool_names(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert len(names) == len(set(names))


def test_client_uses_given_base_url(mock_spec_get, spy_async_client):
    build_server("https://example.test/v1", "user", "pass")
    assert spy_async_client["base_url"] == "https://example.test/v1"


def test_client_uses_basic_auth_with_given_credentials(mock_spec_get, spy_async_client):
    build_server("https://example.test/v1", "alice", "s3cret")
    auth = spy_async_client["auth"]
    assert isinstance(auth, httpx.BasicAuth)
    # httpx.BasicAuth stores credentials pre-encoded; round-trip via the auth header it builds.
    request = httpx.Request("GET", "https://example.test/v1/")
    flow = auth.sync_auth_flow(request)
    signed_request = next(flow)
    assert signed_request.headers["Authorization"].startswith("Basic ")


def test_returns_fastmcp_server_named_signaleyes(mock_spec_get, spy_async_client):
    from fastmcp import FastMCP

    server = build_server("https://example.test/v1", "user", "pass")
    assert isinstance(server, FastMCP)
    assert server.name == "SignalEyes API"


def test_server_has_workflow_instructions(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert server.instructions
    assert "list_source_entities" in server.instructions
    assert "wait_for_task" in server.instructions


def test_spec_fetch_http_error_propagates(monkeypatch):
    def fake_get(url, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(500, json={"detail": "boom"}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        build_server("https://example.test/v1", "user", "pass")
