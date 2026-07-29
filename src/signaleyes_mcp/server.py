"""MCP server exposing NEO's SignalEyes API (SuperAPI) as tools.

Turns https://api.neo.nl/v1's OpenAPI spec into MCP tools automatically via
FastMCP.from_openapi — one call per HTTP operation, no hand-written wrappers.
A few hand-written tools sit on top for things the spec can't express:
task polling, header-driven pagination, and a credential check.

`uv run signaleyes-mcp` also supports two non-serving subcommands:
`test` (runs the test suite) and `smoke` (runs one real query against the
live API to confirm credentials/connectivity work).
"""

import argparse
import asyncio
import os
import random
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType, RouteMap

# Binary/XML endpoints an LLM tool call can't usefully consume: raw OGC WFS,
# vector tiles, DSM/DTM raster tiles (their openapi.json entry claims
# application/json, but they actually return image/tiff — confirmed by
# direct request, not just spec-reading), the shadow-modelling raster, and
# the usage-records XLSX export. Everything else becomes a tool.
EXCLUDED_PATTERNS = (
    r"^/source/wfs/.*",
    r"^/source/tiles/.*",
    r"^/source/(dsm|dtm)(/pages)?(/\{quadkey\})?$",
    r"^/analyses/shadow_modelling$",
    r"^/usage/records$",
)

# These POST /source/* endpoints declare their request body as a bare JSON
# string (the WKT polygon), not an object. FastMCP's OpenAPI request builder
# sends bare-string bodies as raw unencoded bytes with no Content-Type header
# instead of JSON-encoding them, so every one of these auto-generated tools
# 400s upstream with a JSON decode error ("Expecting value") regardless of
# input — confirmed by inspecting the outgoing request. `post_large_polygon`
# below replaces them with one hand-built tool that serializes the body
# correctly; object-bodied POSTs (the */batch id-lookup endpoints,
# /source/geojson) are unaffected and stay auto-generated.
_BROKEN_POLYGON_BODY_POST_PATTERNS = (
    r"^/source/agriparcel$",
    r"^/source/amsparcel$",
    r"^/source/bag$",
    r"^/source/building$",
    r"^/source/building/\{entity\}$",
    r"^/source/building/inactive/\{entity\}$",
    r"^/source/building-risk$",
    r"^/source/building-risk/hr$",
    r"^/source/district$",
    r"^/source/greenspace$",
    r"^/source/imperviousness$",
    r"^/source/landscape-element/\{entity\}$",
    r"^/source/naturearea$",
    r"^/source/neighborhood$",
    r"^/source/road$",
    r"^/source/solarpanel$",
    r"^/source/thirdparty/\{identifier\}$",
    r"^/source/tree$",
    r"^/source/tree/\{entity\}$",
    r"^/source/water$",
)

# A small real area (Venlo, NL) known to have buildings/trees/roads, used by
# `smoke` as a default query when the caller doesn't supply one.
SMOKE_POLYGON = (
    "POLYGON ((5.611503 51.644082, 5.611503 51.646226, "
    "5.61734 51.646226, 5.61734 51.644082, 5.611503 51.644082))"
)
SMOKE_ENTITIES = ("building", "tree", "road", "water", "solarpanel")


def _make_client(base_url: str, username: str, password: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, auth=httpx.BasicAuth(username, password))


def _check_response(resp: httpx.Response) -> None:
    """Like resp.raise_for_status(), but folds the API's JSON `detail` into the error.

    The API returns structured 4xx/5xx bodies (e.g. 422 validation errors)
    that raise_for_status() alone discards — surfacing `detail` gives the
    caller (LLM or human) the actual reason instead of just a status code.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = None
        try:
            detail = resp.json().get("detail")
        except ValueError:
            pass
        if detail:
            raise httpx.HTTPStatusError(
                f"{exc} — {detail}", request=exc.request, response=exc.response
            ) from exc
        raise


def build_server(base_url: str, username: str, password: str) -> FastMCP:
    resp = httpx.get(f"{base_url}/openapi.json", timeout=30)
    resp.raise_for_status()
    spec = resp.json()

    client = _make_client(base_url, username, password)

    route_maps = [RouteMap(pattern=p, mcp_type=MCPType.EXCLUDE) for p in EXCLUDED_PATTERNS]
    route_maps.extend(
        RouteMap(pattern=p, methods=["POST"], mcp_type=MCPType.EXCLUDE)
        for p in _BROKEN_POLYGON_BODY_POST_PATTERNS
    )
    route_maps.append(RouteMap(methods=["GET", "POST", "DELETE"], mcp_type=MCPType.TOOL))

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="SignalEyes API",
        route_maps=route_maps,
    )

    @mcp.tool
    async def wait_for_task(
        task_id: str,
        max_wait: float = 120.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Poll a SignalEyes background task until it finishes, then return its result.

        Many endpoints (concepts, analyses, and source fetches sent with the
        `Prefer: respond-async` header) return a task_id immediately instead
        of a result. This collapses the manual GET /task/{id} polling loop
        into one call instead of many separate tool round-trips.
        """
        try:
            async with asyncio.timeout(max_wait):
                while True:
                    status_resp = await client.get(f"/task/{task_id}")
                    _check_response(status_resp)
                    status_body = status_resp.json()
                    status = status_body["status"]
                    if status == "done":
                        result_resp = await client.get(f"/task/{task_id}/result")
                        _check_response(result_resp)
                        result: dict[str, Any] = result_resp.json()
                        return result
                    if status == "failed":
                        raise RuntimeError(f"Task {task_id} failed: {status_body.get('error')}")
                    await asyncio.sleep(poll_interval)
        except TimeoutError as exc:
            raise TimeoutError(f"Task {task_id} did not complete within {max_wait}s") from exc

    @mcp.tool
    async def fetch_all_pages(
        entity: str,
        polygon: str,
        variant: str | None = None,
        max_pages: int = 50,
        filter_string: str | None = None,
        geo_type: str | None = None,
        simplify: float | None = None,
        sample: float | None = None,
        dissolve: bool | None = None,
        intersect_centroid: bool | None = None,
    ) -> dict[str, Any]:
        """Fetch every page of a GET /source/{entity} query and merge into one FeatureCollection.

        The API paginates via the X-Total-Pages response header, which the
        auto-generated per-endpoint tools can't see (they only surface the
        JSON body) — so fetching a multi-page area otherwise takes one tool
        call per page, driven by trial and error. This does it in one call,
        while still supporting the same filters as the single-page tool
        (filter_string, geo_type, simplify, sample, dissolve,
        intersect_centroid) so pagination and filtering aren't a trade-off.
        """
        path = f"/source/{entity}/{variant}" if variant else f"/source/{entity}"
        extra_params: dict[str, Any] = {
            "filter_string": filter_string,
            "geo_type": geo_type,
            "simplify": simplify,
            "sample": sample,
            "dissolve": dissolve,
            "intersect_centroid": intersect_centroid,
        }
        extra_params = {k: v for k, v in extra_params.items() if v is not None}

        features: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= min(total_pages, max_pages):
            resp = await client.get(path, params={"polygon": polygon, "page": page, **extra_params})
            _check_response(resp)
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
    async def post_large_polygon(
        entity: str,
        polygon: str,
        variant: str | None = None,
        respond_async: bool = False,
        filter_string: str | None = None,
        page: int = 1,
        geo_type: str | None = None,
        simplify: float | None = None,
        sample: float | None = None,
        dissolve: bool | None = None,
        intersect_centroid: bool | None = None,
    ) -> dict[str, Any]:
        """POST /source/{entity} with the polygon in the body — for AOIs too large for a GET query.

        Replaces the auto-generated post_objects_*_post tools, which always
        fail: the API declares these request bodies as a bare JSON string
        (just the WKT polygon, not `{"polygon": ...}`), and FastMCP's OpenAPI
        client sends bare-string bodies as raw unencoded bytes with no
        Content-Type instead of JSON-encoding them, so the upstream API
        rejects every call with a JSON decode error. This tool builds the
        request by hand so the body is serialized correctly.

        Set respond_async=True for large AOIs — the API replies with a task
        ID immediately instead of gathering every page inline; resolve it
        with wait_for_task.

        Like the GET endpoint, results are paginated; the response carries
        `total_pages`/`page` (from the X-Total-Pages/X-Page-Number headers)
        so the caller knows whether to fetch more with `page=2`, etc. Covers
        every entity/variant reachable as /source/{entity}[/{variant}]
        except /source/building/inactive/{entity} (three path segments,
        outside this tool's scheme — same gap fetch_all_pages already has).
        """
        path = f"/source/{entity}/{variant}" if variant else f"/source/{entity}"
        params: dict[str, Any] = {
            "filter_string": filter_string,
            "page": page,
            "geo_type": geo_type,
            "simplify": simplify,
            "sample": sample,
            "dissolve": dissolve,
            "intersect_centroid": intersect_centroid,
        }
        params = {k: v for k, v in params.items() if v is not None}
        headers = {"Prefer": "respond-async"} if respond_async else {}

        resp = await client.post(path, params=params, json=polygon, headers=headers)
        _check_response(resp)
        result: dict[str, Any] = resp.json()
        if "X-Total-Pages" in resp.headers:
            result["total_pages"] = int(resp.headers["X-Total-Pages"])
            result["page"] = int(resp.headers.get("X-Page-Number", page))
        return result

    @mcp.tool
    async def whoami() -> dict[str, Any]:
        """Check SignalEyes credentials by fetching the caller's active subscriptions."""
        resp = await client.get("/subscriptions")
        _check_response(resp)
        body: dict[str, Any] = resp.json()
        return body

    return mcp


async def run_smoke(
    base_url: str,
    username: str,
    password: str,
    entity: str | None = None,
    polygon: str = SMOKE_POLYGON,
) -> int:
    """Run one real query against SignalEyes: checks credentials and connectivity end to end.

    Unlike `test`, this hits the live API — useful to sanity-check a
    deployment's env vars without spinning up an MCP client.
    """
    entity = entity or random.choice(SMOKE_ENTITIES)
    async with _make_client(base_url, username, password) as client:
        who_resp = await client.get("/subscriptions")
        _check_response(who_resp)
        print(f"Credentials OK. Subscriptions: {who_resp.json()}")

        query_resp = await client.get(f"/source/{entity}", params={"polygon": polygon, "page": 1})
        _check_response(query_resp)
        data = query_resp.json()
        total_pages = query_resp.headers.get("X-Total-Pages", "1")
        print(
            f"Sample query GET /source/{entity}: "
            f"{len(data.get('features', []))} feature(s) on page 1 of {total_pages}"
        )
    return 0


def run_tests() -> int:
    """Run the test suite via `uv run signaleyes-mcp test` (equivalent to `uv run pytest`)."""
    import pytest  # local: pytest is a dev-only dependency, not needed to serve

    result: int = pytest.main(["-q"])
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signaleyes-mcp")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server over stdio (default)")
    sub.add_parser("test", help="Run the test suite")
    smoke = sub.add_parser(
        "smoke", help="Run one real query against SignalEyes to check the setup works"
    )
    smoke.add_argument(
        "--entity", choices=SMOKE_ENTITIES, default=None, help="Defaults to a random entity"
    )
    smoke.add_argument("--polygon", default=SMOKE_POLYGON, help="WKT polygon in EPSG:4326")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    command = args.command or "serve"

    if command == "test":
        raise SystemExit(run_tests())

    # Fills in SIGNALEYES_* from a .env file in the cwd (or a parent dir) if
    # present; real environment variables (e.g. set by an MCP client's own
    # config) still win, since load_dotenv() never overrides existing ones.
    load_dotenv()

    base_url = os.environ.get("SIGNALEYES_BASE_URL", "https://api.neo.nl/v1")
    username = os.environ.get("SIGNALEYES_USERNAME")
    password = os.environ.get("SIGNALEYES_PASSWORD")
    if not username or not password:
        print(
            "SIGNALEYES_USERNAME and SIGNALEYES_PASSWORD must be set (SignalEyes API credentials).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if command == "smoke":
        raise SystemExit(
            asyncio.run(run_smoke(base_url, username, password, args.entity, args.polygon))
        )

    server = build_server(base_url, username, password)
    server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
