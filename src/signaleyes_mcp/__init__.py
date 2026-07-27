"""MCP server exposing NEO's SignalEyes API (SuperAPI) as tools.

Turns https://api.neo.nl/v1's OpenAPI spec into MCP tools automatically via
FastMCP.from_openapi — one call per HTTP operation, no hand-written wrappers.
"""

import os
import sys

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

    return FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="SignalEyes API",
        route_maps=route_maps,
    )


def main() -> None:
    base_url = os.environ.get("SIGNALEYES_BASE_URL", "https://api.neo.nl/v1")
    username = os.environ.get("SIGNALEYES_USERNAME")
    password = os.environ.get("SIGNALEYES_PASSWORD")
    if not username or not password:
        print(
            "SIGNALEYES_USERNAME and SIGNALEYES_PASSWORD must be set "
            "(SignalEyes API credentials).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    server = build_server(base_url, username, password)
    server.run()


if __name__ == "__main__":
    main()
