from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest


def make_spec(paths: dict) -> dict:
    return {"openapi": "3.0.0", "info": {"title": "test", "version": "1"}, "paths": paths}


def op(operation_id: str, path_params: tuple[str, ...] = ()) -> dict:
    return {
        "operationId": operation_id,
        "parameters": [
            {"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
            for p in path_params
        ],
        "responses": {"200": {"description": "ok"}},
    }


@pytest.fixture
def fake_spec() -> dict:
    """A small spec covering every route-map branch build_server has to handle.

    Mirrors the real API's shape for the paths that matter to the
    registry-derivation tests: a plain entity (agriparcel: list/by_id/
    metadata/batch, no variant), a variant-optional entity (building: works
    both bare and with a sub-type), and a three-segment variant-required
    entity (building/inactive) — the case fetch_all_pages/post_large_polygon
    can't reach via their own entity/variant scheme.
    """
    return make_spec(
        {
            "/source/wfs/tree": {"get": op("wfs_tree")},
            "/source/tiles/{id}/{z}/{x}/{y}": {
                "get": op("tiles", ("id", "z", "x", "y")),
            },
            "/source/dsm/{quadkey}": {"get": op("dsm_quadkey", ("quadkey",))},
            "/source/dtm/{quadkey}": {"get": op("dtm_quadkey", ("quadkey",))},
            "/source/dsm/pages": {"get": op("dsm_pages")},
            "/source/dsm/pages/metadata": {"get": op("dsm_pages_metadata")},
            "/analyses/shadow_modelling": {"get": op("shadow_modelling")},
            "/usage/records": {"get": op("usage_records")},
            "/subscriptions": {"get": op("get_subscriptions")},
            "/token/generate": {"post": op("token_generate")},
            "/token/refresh": {"post": op("token_refresh")},
            "/token/revoke": {"post": op("token_revoke")},
            "/source/building": {"get": op("get_building"), "post": op("post_building")},
            "/source/building/{entity}": {
                "get": op("get_building_entity", ("entity",)),
                "post": op("post_building_entity", ("entity",)),
            },
            "/source/building/{entity}/{id}": {"get": op("get_building_by_id", ("entity", "id"))},
            "/source/building/{entity}/metadata": {
                "get": op("get_building_entity_metadata", ("entity",))
            },
            "/source/building/{entity}/batch": {"post": op("post_building_batch", ("entity",))},
            "/source/building/inactive/{entity}": {
                "get": op("get_building_inactive", ("entity",)),
            },
            "/source/building/inactive/{entity}/{id}": {
                "get": op("get_building_inactive_by_id", ("entity", "id"))
            },
            "/source/building/inactive/{entity}/metadata": {
                "get": op("get_building_inactive_metadata", ("entity",))
            },
            "/source/building/inactive/{entity}/batch": {
                "post": op("post_building_inactive_batch", ("entity",))
            },
            "/source/agriparcel": {"get": op("get_agriparcel"), "post": op("post_agriparcel")},
            "/source/agriparcel/{id}": {"get": op("get_agriparcel_by_id", ("id",))},
            "/source/agriparcel/metadata": {"get": op("get_agriparcel_metadata")},
            "/source/agriparcel/batch": {"post": op("post_agriparcel_batch")},
            "/task/{task_id}": {
                "get": op("get_task", ("task_id",)),
                "delete": op("delete_task", ("task_id",)),
            },
            "/task/{task_id}/describe": {"get": op("describe_task", ("task_id",))},
            "/task/{task_id}/metadata": {"get": op("task_metadata", ("task_id",))},
            "/task/{task_id}/result": {"get": op("task_result", ("task_id",))},
        }
    )


@pytest.fixture
def mock_spec_get(monkeypatch, fake_spec):
    """Patch httpx.get so build_server() never hits the network. Returns a call recorder."""
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=fake_spec, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


@pytest.fixture
def spy_async_client(monkeypatch):
    """Patch httpx.AsyncClient to record constructor kwargs while staying a real client."""
    captured = {}
    real_init = httpx.AsyncClient.__init__

    def spying_init(self, *args, **kwargs):
        captured["base_url"] = kwargs.get("base_url")
        captured["auth"] = kwargs.get("auth")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spying_init)
    return captured


@pytest.fixture
def async_client_get_queue(monkeypatch):
    """Patch httpx.AsyncClient.get to pop pre-queued responses in call order.

    Tests append httpx.Response objects to `.responses` before invoking the
    tool under test; `.calls` records each (url, params) pair made.
    """
    state = SimpleNamespace(responses=[], calls=[])

    async def fake_get(self, url, params=None, headers=None, **kwargs):
        state.calls.append({"url": url, "params": params, "headers": headers})
        return state.responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return state


@pytest.fixture
def async_client_post_queue(monkeypatch):
    """Like async_client_get_queue, but for POST — also records the json body sent."""
    state = SimpleNamespace(responses=[], calls=[])

    async def fake_post(self, url, params=None, json=None, headers=None, **kwargs):
        state.calls.append({"url": url, "params": params, "json": json, "headers": headers})
        return state.responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return state


@pytest.fixture
def fast_sleep(monkeypatch):
    """Skip real delays in asyncio.sleep so polling-loop tests run instantly.

    Also replaces asyncio.timeout with a virtual-time version so deadlines
    still fire when sleep is a no-op against the wall clock.
    """
    elapsed = {"t": 0.0}
    deadline = {"t": None}

    async def instant_sleep(seconds: float) -> None:
        elapsed["t"] += seconds
        if deadline["t"] is not None and elapsed["t"] >= deadline["t"]:
            raise TimeoutError()

    @asynccontextmanager
    async def instant_timeout(delay: float) -> AsyncIterator[None]:
        deadline["t"] = elapsed["t"] + delay
        try:
            yield
        finally:
            deadline["t"] = None

    monkeypatch.setattr("signaleyes_mcp.server.asyncio.sleep", instant_sleep)
    monkeypatch.setattr("signaleyes_mcp.server.asyncio.timeout", instant_timeout)
