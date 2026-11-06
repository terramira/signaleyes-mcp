from __future__ import annotations

import asyncio

import httpx
import pytest
from spec_helpers import make_spec, op

from signaleyes_mcp import build_server
from signaleyes_mcp.spec import MCP_NAMES


def _task_response(task_id: str, status: str, error: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"id": task_id, "status": status, "error": error},
        request=httpx.Request("GET", "https://example.test/"),
    )


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


def test_thirdparty_arrow_excluded(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert not any("arrow" in n for n in _tool_names(server))


def test_duplicate_post_concepts_excluded(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    names = _tool_names(server)
    assert "concept_closest" in names
    assert "post_closest" not in names


def test_pagination_boilerplate_trimmed_from_descriptions(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    tool = asyncio.run(server.get_tool("concept_closest"))
    assert tool.description == "Find closest."


def test_spec_cache_is_written_and_reused(monkeypatch, tmp_path, mock_spec_get, spy_async_client):
    cache = tmp_path / "spec.json"
    monkeypatch.setenv("SIGNALEYES_SPEC_CACHE", str(cache))

    build_server("https://example.test/v1", "user", "pass")
    build_server("https://example.test/v1", "user", "pass")

    assert cache.exists()
    assert len(mock_spec_get) == 1


def test_stale_spec_cache_used_when_fetch_fails(monkeypatch, tmp_path, fake_spec):
    import json
    import os

    cache = tmp_path / "spec.json"
    cache.write_text(json.dumps(fake_spec))
    os.utime(cache, (0, 0))
    monkeypatch.setenv("SIGNALEYES_SPEC_CACHE", str(cache))

    def fake_get(url, timeout=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", fake_get)

    server = build_server("https://example.test/v1", "user", "pass")
    assert "fetch_all_pages" in _tool_names(server)


def test_list_source_entities_reports_variant_enums(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    out = asyncio.run(server.call_tool("list_source_entities", {})).structured_content
    assert out["building"]["variants"] == ["detail", "surrounding"]
    assert "variants" not in out["agriparcel"]


def test_concept_call_waits_for_queued_input_tasks(
    mock_spec_get, spy_async_client, async_client_get_queue, fast_sleep, monkeypatch
):
    from types import SimpleNamespace

    from signaleyes_mcp.server import _AwaitInputTasks

    server = build_server("https://example.test/v1", "user", "pass")
    mw = next(m for m in server.middleware if isinstance(m, _AwaitInputTasks))
    state = {"a": iter(["queued", "done"]), "b": iter(["done"])}

    async def fake_get(self, url, **kwargs):
        async_client_get_queue.calls.append({"url": url})
        task = url.rsplit("/", 1)[1]
        return _task_response(task, next(state[task]))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    forwarded = []

    async def call_next(ctx):
        forwarded.append(ctx)
        return "result"

    ctx = SimpleNamespace(
        message=SimpleNamespace(
            name="concept_closest",
            arguments={"task_id_primary_entity": "a", "task_id_secondary_entity": "b", "radius": 1},
        )
    )
    assert asyncio.run(mw.on_call_tool(ctx, call_next)) == "result"
    assert sorted(c["url"] for c in async_client_get_queue.calls) == [
        "/task/a",
        "/task/a",
        "/task/b",
    ]
    assert forwarded == [ctx]


def test_concept_call_surfaces_failed_input_task(
    mock_spec_get, spy_async_client, async_client_get_queue
):
    from types import SimpleNamespace

    from fastmcp.exceptions import ToolError

    from signaleyes_mcp.server import _AwaitInputTasks

    server = build_server("https://example.test/v1", "user", "pass")
    mw = next(m for m in server.middleware if isinstance(m, _AwaitInputTasks))
    async_client_get_queue.responses.append(_task_response("a", "failed", "boom"))
    ctx = SimpleNamespace(
        message=SimpleNamespace(name="concept_closest", arguments={"task_id_primary_entity": "a"})
    )

    async def call_next(ctx):
        raise AssertionError("must not forward")

    with pytest.raises(ToolError, match="boom"):
        asyncio.run(mw.on_call_tool(ctx, call_next))


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


def test_client_uses_a_longer_than_default_timeout(mock_spec_get, spy_async_client):
    """httpx's 5s default has been observed too tight for /describe, /result, and
    delete_task on this API — a merely-slow (not stuck) request shouldn't read as
    a hard failure to every tool sharing this client."""
    build_server("https://example.test/v1", "user", "pass")
    timeout = spy_async_client["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 60.0


def test_mcp_names_shortens_every_mapped_operation(monkeypatch, spy_async_client):
    """Concept/analyses/task/geojson tools stay auto-generated, but FastMCP's
    default name is the raw operationId — FastAPI's, in particular, repeat the
    function name and path. Build a spec covering every real long operationId
    MCP_NAMES maps (rather than the short synthetic ones the shared fake_spec
    fixture uses) and confirm each comes out as its mapped short name. Covers
    every entry, not just one, since MCP_NAMES is hand-maintained — a
    stale/typo'd key silently falls back to the raw, unshortened name instead
    of erroring, so only exercising the whole mapping catches that in CI."""
    spec = make_spec(
        {f"/synthetic/{i}": {"get": op(long_id)} for i, long_id in enumerate(MCP_NAMES)}
    )

    def fake_get(url, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=spec, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)

    server = build_server("https://example.test/v1", "user", "pass")
    names = set(_tool_names(server))
    for long_id, short_name in MCP_NAMES.items():
        assert short_name in names, f"{long_id} -> {short_name} did not take effect"
        assert long_id not in names, f"{long_id} leaked through unshortened"


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
    assert "skill://signaleyes/SKILL.md" in server.instructions


def test_spec_fetch_http_error_propagates(monkeypatch):
    def fake_get(url, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(500, json={"detail": "boom"}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        build_server("https://example.test/v1", "user", "pass")


def test_client_is_the_retrying_client_with_default_transport(mock_spec_get, spy_async_client):
    from signaleyes_mcp.client import RetryingClient

    server = build_server("https://example.test/v1", "user", "pass")
    client = next(m for m in server.middleware if hasattr(m, "_client"))._client
    assert isinstance(client, RetryingClient)
    # No custom transport: httpx keeps HTTP(S)_PROXY / NO_PROXY support.
    assert spy_async_client["transport"] is None


def test_exposes_signaleyes_skill_resource(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    resources = asyncio.run(server.list_resources())
    uris = [str(r.uri) for r in resources]
    assert any(u.startswith("skill://signaleyes/") for u in uris)


def test_tools_carry_read_vs_destructive_annotations(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    assert tools["list_source_entities"].annotations is not None
    assert tools["list_source_entities"].annotations.readOnlyHint is True
    assert tools["list_source_entities"].annotations.openWorldHint is False
    assert tools["fetch_all_pages"].annotations.readOnlyHint is True
    assert tools["post_large_polygon"].annotations.readOnlyHint is False
    assert tools["delete_task"].annotations is not None
    assert tools["delete_task"].annotations.destructiveHint is True


def test_concept_middleware_ignores_tools_without_task_inputs(mock_spec_get, spy_async_client):
    from types import SimpleNamespace

    from signaleyes_mcp.server import _AwaitInputTasks

    server = build_server("https://example.test/v1", "user", "pass")
    mw = next(m for m in server.middleware if isinstance(m, _AwaitInputTasks))
    ctx = SimpleNamespace(
        message=SimpleNamespace(name="fetch_all_pages", arguments={"task_id_primary_entity": "a"})
    )

    async def call_next(ctx):
        return "forwarded without polling"

    assert asyncio.run(mw.on_call_tool(ctx, call_next)) == "forwarded without polling"


def test_generated_tool_errors_are_tagged(mock_spec_get, spy_async_client, async_client_get_queue):
    """The tag middleware wraps every tool, so the orchestrator never parses error prose."""
    server = build_server("https://example.test/v1", "user", "pass")
    async_client_get_queue.responses.append(
        httpx.Response(
            409,
            json={"detail": "Task x is not done yet"},
            request=httpx.Request("GET", "https://example.test/"),
        )
    )
    with pytest.raises(Exception, match=r"wait_for_task\. \[retryable=yes\]$"):
        asyncio.run(server.call_tool("manage_task", {"task_id": "x", "view": "result"}))


def test_spec_cache_is_keyed_by_base_url_and_survives_corruption(monkeypatch, tmp_path, fake_spec):
    import json

    from signaleyes_mcp.spec import _cache_path, load_spec

    monkeypatch.delenv("SIGNALEYES_SPEC_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    a, b = _cache_path("https://a.test/v1"), _cache_path("https://b.test/v1")
    assert a != b and a.parent == b.parent

    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return httpx.Response(200, json=fake_spec, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    load_spec("https://a.test/v1")
    assert json.loads(a.read_text()) == fake_spec
    a.write_text("{truncated")
    load_spec("https://a.test/v1")  # corrupt cache is a miss, not a crash
    assert len(calls) == 2
    assert json.loads(a.read_text()) == fake_spec
