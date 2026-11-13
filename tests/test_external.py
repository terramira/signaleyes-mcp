from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2
import pytest
from fastmcp.exceptions import ToolError

from signaleyes_mcp import build_server, external

URL = "https://provider.test/wfs"
AOI = "POLYGON((5.38 52.15,5.39 52.15,5.39 52.16,5.38 52.16,5.38 52.15))"
GEOJSON = {"type": "FeatureCollection", "features": []}


@pytest.fixture
def server(mock_spec_get, spy_async_client):
    return build_server("https://example.test/v1", "user", "pass")


def call(server, name, **args):
    return asyncio.run(server.call_tool(name, args)).structured_content


def fake_core(monkeypatch, count=1, error=None):
    calls = []

    def download(*args):
        calls.append(args)
        if error:
            raise ValueError(error)
        return {"geojson": GEOJSON, "feature_count": count, "protocol": "wfs"}

    core = SimpleNamespace(download_source=download, inspect_source=lambda *args: {"layers": []})
    monkeypatch.setattr(external, "_load_core", lambda: core)
    return calls


def test_import_uploads_geojson_once_with_existing_client(
    server, monkeypatch, async_client_post_queue
):
    calls = fake_core(monkeypatch)
    async_client_post_queue.responses.append(
        httpx2.Response(202, json={"id": "import-123", "status": "queued"})
    )
    result = call(server, "import_external_dataset", url=URL, layer="bag:pand", polygon=AOI)
    assert calls == [(URL, "bag:pand", AOI, "auto")]
    assert async_client_post_queue.calls == [
        {
            "url": "/source/geojson",
            "params": None,
            "json": GEOJSON,
            "headers": {"Prefer": "respond-async"},
        }
    ]
    assert result["task_id"] == "import-123"
    assert result["next_tool"] == "manage_task"
    assert "geojson" not in result


def test_empty_import_creates_no_task(server, monkeypatch, async_client_post_queue):
    fake_core(monkeypatch, count=0)
    result = call(server, "import_external_dataset", url=URL, layer="bag:pand", polygon=AOI)
    assert result["status"] == "empty"
    assert "task_id" not in result
    assert not async_client_post_queue.calls


def test_rejected_download_never_uploads(server, monkeypatch, async_client_post_queue):
    fake_core(monkeypatch, error="Private addresses are blocked")
    with pytest.raises(ToolError, match="Private addresses"):
        call(server, "import_external_dataset", url=URL, layer="bag:pand", polygon=AOI)
    assert not async_client_post_queue.calls


def test_discovery_never_uses_authenticated_client(server, monkeypatch, async_client_post_queue):
    fake_core(monkeypatch)
    assert call(server, "inspect_external_dataset", url=URL) == {"layers": []}
    assert not async_client_post_queue.calls


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_upload_failure_is_not_reported_as_a_task(
    server, monkeypatch, async_client_post_queue, status
):
    fake_core(monkeypatch)
    async_client_post_queue.responses.append(httpx2.Response(status))
    with pytest.raises(ToolError, match=f"HTTP {status}"):
        call(server, "import_external_dataset", url=URL, layer="bag:pand", polygon=AOI)


def test_missing_task_id_does_not_reupload(server, monkeypatch, async_client_post_queue):
    fake_core(monkeypatch)
    async_client_post_queue.responses.append(httpx2.Response(202, json={"status": "queued"}))
    with pytest.raises(ToolError, match="check tasks before retrying"):
        call(server, "import_external_dataset", url=URL, layer="bag:pand", polygon=AOI)
    assert len(async_client_post_queue.calls) == 1


def test_external_tools_have_correct_side_effect_annotations(server):
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert tools["inspect_external_dataset"].annotations.read_only_hint is True
    assert tools["import_external_dataset"].annotations.read_only_hint is False
    assert tools["import_external_dataset"].annotations.idempotent_hint is False
    assert tools["export_task_file"].annotations.read_only_hint is False
