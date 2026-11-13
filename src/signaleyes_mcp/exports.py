"""Save completed task geometry without returning it through the MCP conversation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from signaleyes_mcp.spec import WRITE_OPEN

MAX_EXPORT_BYTES = 50_000_000


def output_dir() -> Path:
    path = Path(os.environ.get("SIGNALEYES_OUTPUT_DIR", Path.home() / "signaleyes_outputs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def register_export_tools(mcp: FastMCP, client: httpx2.AsyncClient) -> None:
    @mcp.tool(annotations=WRITE_OPEN)
    async def export_task_file(task_id: str) -> dict[str, Any]:
        """Save a completed SignalEyes task as a local GeoJSON file, not inline data.

        Use this instead of manage_task(view="result") or wait_for_task when
        geometries would exceed the chat tool limit. Uses the existing login;
        no credentials or geometries are returned. Only completed tasks, up to
        50 MB; no new task or centroid calculation is created.

        The saved file can be opened in a GIS application. A local path is
        not a hosted URL or an uploaded attachment. Credentials remain local;
        do not claim the file has been uploaded or shared automatically.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ToolError("Use the task_id returned by SignalEyes.")
        path: Path | None = None
        complete = False
        try:
            async with asyncio.timeout(120):
                status = await client.get(f"/task/{task_id}", follow_redirects=False)
                if status.status_code != 200:
                    raise ToolError(f"Task status returned HTTP {status.status_code}.")
                if status.json().get("status") != "done":
                    raise ToolError(
                        "Only completed tasks can be exported. Check manage_task first."
                    )
                async with client.stream(
                    "GET", f"/task/{task_id}/result", follow_redirects=False
                ) as response:
                    if response.status_code != 200:
                        raise ToolError(f"Task download returned HTTP {response.status_code}.")
                    size = 0
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        prefix=f"task_{task_id}_",
                        suffix=".geojson",
                        dir=output_dir(),
                        delete=False,
                    ) as output:
                        path = Path(output.name)
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_EXPORT_BYTES:
                                raise ToolError("Result exceeds the 50 MB export limit.")
                            output.write(chunk)
                with path.open(encoding="utf-8-sig") as source:
                    data = json.load(source)
                if (
                    not isinstance(data, dict)
                    or data.get("type") != "FeatureCollection"
                    or not isinstance(data.get("features"), list)
                ):
                    raise ToolError("Task result is not a GeoJSON FeatureCollection.")
                result = {
                    "task_id": task_id,
                    "path": str(path.resolve()),
                    "filename": path.name,
                    "size_bytes": size,
                    "feature_count": len(data["features"]),
                    "next_step": "Open the saved GeoJSON file in your GIS application.",
                }
                complete = True
                return result
        except httpx2.HTTPError as exc:
            raise ToolError("Task download failed; the existing task is unchanged.") from exc
        except TimeoutError as exc:
            raise ToolError("Export timed out; the existing task is unchanged.") from exc
        except (ValueError, AttributeError) as exc:
            raise ToolError(
                "SignalEyes returned an invalid task status or GeoJSON result."
            ) from exc
        except OSError as exc:
            raise ToolError(
                "Could not save the task file; check the output directory and disk space."
            ) from exc
        finally:
            if path is not None and not complete:
                path.unlink(missing_ok=True)
