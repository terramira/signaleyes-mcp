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


def test_dsm_pages_not_excluded_by_quadkey_pattern(mock_spec_get, spy_async_client):
    """Only the raw raster quadkey routes are excluded — sibling metadata routes stay."""
    server = build_server("https://example.test/v1", "user", "pass")
    assert "dsm_pages" in _tool_names(server)


def test_normal_source_endpoints_become_tools(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert "get_building" in names
    assert "post_building" in names


def test_get_and_delete_endpoints_become_tools(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert "get_task" in names
    assert "delete_task" in names


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


def test_spec_fetch_http_error_propagates(monkeypatch):
    def fake_get(url, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(500, json={"detail": "boom"}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        build_server("https://example.test/v1", "user", "pass")
