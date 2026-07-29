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
    """A small spec covering every route-map branch build_server has to handle."""
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
            "/source/building": {"get": op("get_building"), "post": op("post_building")},
            "/task/{task_id}": {
                "get": op("get_task", ("task_id",)),
                "delete": op("delete_task", ("task_id",)),
            },
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

    async def fake_get(self, url, params=None, **kwargs):
        state.calls.append({"url": url, "params": params})
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
