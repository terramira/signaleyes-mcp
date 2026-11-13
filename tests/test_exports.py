from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from signaleyes_mcp import build_server, exports

FEATURES = [
    {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [5, 52]},
        "properties": {"name": "Example"},
    }
]
GEOJSON = {"type": "FeatureCollection", "features": FEATURES}


def export(tmp_path, monkeypatch, *, state="done", payload=GEOJSON, limit=None, task_id="clip-123"):
    monkeypatch.setattr(exports, "output_dir", lambda: tmp_path)
    if limit is not None:
        monkeypatch.setattr(exports, "MAX_EXPORT_BYTES", limit)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/result"):
            return httpx2.Response(200, json=payload)
        return httpx2.Response(200, json={"status": state})

    async def run():
        async with httpx2.AsyncClient(
            base_url="https://example.test", transport=httpx2.MockTransport(handler)
        ) as client:
            server = FastMCP("export")
            exports.register_export_tools(server, client)
            result = await server.call_tool("export_task_file", {"task_id": task_id})
            return result.structured_content

    return asyncio.run(run()), calls


def test_export_tool_is_registered_on_signaleyes(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")
    assert "export_task_file" in {tool.name for tool in asyncio.run(server.list_tools())}


def test_export_saves_original_features_without_returning_geometry(tmp_path, monkeypatch):
    result, calls = export(tmp_path, monkeypatch)
    assert calls == ["/task/clip-123", "/task/clip-123/result"]
    assert json.loads(Path(result["path"]).read_text()) == GEOJSON
    assert result["feature_count"] == 1
    assert result["size_bytes"] == Path(result["path"]).stat().st_size
    assert "features" not in result and "geojson" not in result


def test_large_chat_result_is_saved_without_chat_payload(tmp_path, monkeypatch):
    payload = {
        "type": "FeatureCollection",
        "features": [{**FEATURES[0], "properties": {"description": "x" * 1_100_000}}],
    }
    result, _ = export(tmp_path, monkeypatch, payload=payload)
    assert result["size_bytes"] > 1_000_000
    assert len(json.dumps(result)) < 2000


def test_export_refuses_incomplete_task(tmp_path, monkeypatch):
    with pytest.raises(ToolError, match="Only completed"):
        export(tmp_path, monkeypatch, state="running")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("payload,limit", [(GEOJSON, 1), ({"error": "not geometry"}, None)])
def test_failed_export_removes_partial_file(tmp_path, monkeypatch, payload, limit):
    with pytest.raises(ToolError):
        export(tmp_path, monkeypatch, payload=payload, limit=limit)
    assert not list(tmp_path.iterdir())


def test_task_id_cannot_escape_output_directory(tmp_path, monkeypatch):
    with pytest.raises(ToolError, match="task_id"):
        export(tmp_path, monkeypatch, task_id="../other")
    assert not list(tmp_path.iterdir())
