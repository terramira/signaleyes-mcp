"""MCP server exposing NEO's SignalEyes API (SuperAPI) as tools.

Turns https://api.neo.nl/v1's OpenAPI spec into MCP tools automatically via
FastMCP.from_openapi — one call per HTTP operation, no hand-written wrappers.
A few hand-written tools sit on top for things the spec can't express:
task polling, header-driven pagination, and a credential check.
"""

import asyncio
import os
import sys
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

# Binary/XML endpoints an LLM tool call can't usefully consume: raw OGC WFS,
# vector tiles, and DSM/DTM raster tiles. Everything else becomes a tool.
EXCLUDED_PATTERNS = (
    r"^/source/wfs/.*",
    r"^/source/tiles/.*",
    r"^/source/(dsm|dtm)/\{quadkey\}$",
)


def build_server(base_url: str, username: str, password: str) -> FastMCP:
    resp = httpx.get(f"{base_url}/openapi.json", timeout=30)
    resp.raise_for_status()
    spec = resp.json()

    client = httpx.AsyncClient(base_url=base_url, auth=httpx.BasicAuth(username, password))

    route_maps = [RouteMap(pattern=p, mcp_type=MCPType.EXCLUDE) for p in EXCLUDED_PATTERNS]
    route_maps.append(RouteMap(methods=["GET", "POST", "DELETE"], mcp_type=MCPType.TOOL))

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="SignalEyes API",
        route_maps=route_maps,
    )

    @mcp.tool
    async def wait_for_task(
        task_id: str, timeout: float = 120.0, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        """Poll a SignalEyes background task until it finishes, then return its result.

        Many endpoints (concepts, analyses, and source fetches sent with the
        `Prefer: respond-async` header) return a task_id immediately instead
        of a result. This collapses the manual GET /task/{id} polling loop
        into one call instead of many separate tool round-trips.
        """
        max_polls = max(1, int(timeout / poll_interval))
        for _ in range(max_polls):
            status_resp = await client.get(f"/task/{task_id}")
            status_resp.raise_for_status()
            status_body = status_resp.json()
            status = status_body["status"]
            if status == "done":
                result_resp = await client.get(f"/task/{task_id}/result")
                result_resp.raise_for_status()
                result: dict[str, Any] = result_resp.json()
                return result
            if status == "failed":
                raise RuntimeError(f"Task {task_id} failed: {status_body.get('error')}")
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")

    @mcp.tool
    async def fetch_all_pages(
        entity: str, polygon: str, variant: str | None = None, max_pages: int = 50
    ) -> dict[str, Any]:
        """Fetch every page of a GET /source/{entity} query and merge into one FeatureCollection.

        The API paginates via the X-Total-Pages response header, which the
        auto-generated per-endpoint tools can't see (they only surface the
        JSON body) — so fetching a multi-page area otherwise takes one tool
        call per page, driven by trial and error. This does it in one call.
        """
        path = f"/source/{entity}/{variant}" if variant else f"/source/{entity}"
        features: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= min(total_pages, max_pages):
            resp = await client.get(path, params={"polygon": polygon, "page": page})
            resp.raise_for_status()
            total_pages = int(resp.headers.get("X-Total-Pages", 1))
            features.extend(resp.json().get("features", []))
            page += 1
        return {
            "type": "FeatureCollection",
            "features": features,
            "total_pages": total_pages,
            "pages_fetched": min(total_pages, max_pages),
        }

    @mcp.tool
    async def whoami() -> dict[str, Any]:
        """Check SignalEyes credentials by fetching the caller's active subscriptions."""
        resp = await client.get("/subscriptions")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    return mcp


def main() -> None:
    base_url = os.environ.get("SIGNALEYES_BASE_URL", "https://api.neo.nl/v1")
    username = os.environ.get("SIGNALEYES_USERNAME")
    password = os.environ.get("SIGNALEYES_PASSWORD")
    if not username or not password:
        print(
            "SIGNALEYES_USERNAME and SIGNALEYES_PASSWORD must be set (SignalEyes API credentials).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    server = build_server(base_url, username, password)
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
