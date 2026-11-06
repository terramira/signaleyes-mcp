"""MCP server exposing NEO's SignalEyes API (SuperAPI) as tools.

Turns https://api.neo.nl/v1's OpenAPI spec into MCP tools via
FastMCP.from_openapi. Most operations are generated automatically; the
hand-written tools here cover what the spec can't express (task polling,
header-driven pagination, credential check) and collapse the per-entity
operations into generic `entity`-parameterised ones. `spec.py` decides what
is exposed, `client.py` talks to the API; this module holds the tools and
the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.skills import SkillProvider
from mcp.types import CallToolRequestParams

from signaleyes_mcp.client import (
    TaskFailed,
    check_response,
    make_client,
    poll_until_done,
    tag_error,
    task_status,
)
from signaleyes_mcp.spec import (
    DESTRUCTIVE_OPEN,
    MCP_NAMES,
    READ_LOCAL,
    READ_OPEN,
    TASK_INPUT_ARGS,
    WRITE_OPEN,
    annotate_openapi_component,
    build_route_maps,
    build_source_registry,
    describe_source_registry,
    load_spec,
    resolve_source_path,
    task_input_tools,
    write_tool_names,
)

__all__ = ["DESTRUCTIVE_OPEN", "MCP_NAMES", "build_server", "main"]

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# Claude Code shunts tool results above ~25k tokens to a file the model never
# sees inline. Coordinate-heavy GeoJSON tokenizes at ~3 chars/token, so a
# 95 KB result still went to file in testing; 50 KB stays inline.
_DEFAULT_MAX_RESPONSE_BYTES = 50_000

# A small real area (Venlo, NL) known to have buildings/trees/roads, used by
# `smoke` as a default query when the caller doesn't supply one.
SMOKE_POLYGON = (
    "POLYGON ((5.611503 51.644082, 5.611503 51.646226, "
    "5.61734 51.646226, 5.61734 51.644082, 5.611503 51.644082))"
)
SMOKE_ENTITIES = ("building", "tree", "road", "water", "solarpanel")

SERVER_INSTRUCTIONS = """\
SignalEyes: geospatial data (buildings, trees, roads, water, solar panels, \
...) plus chainable analytical concepts (density, closest, buffer, overlay, \
sjoin, ...) over an area-of-interest polygon.

Read resource skill://signaleyes/SKILL.md before the first tool call — it is
the workflow for this API (when to fetch vs describe, GET vs POST concepts,
the geojson_list shape).

Typical flow:
1. Unsure which dataset/entity to use? Call `list_source_entities` — it
   reflects whatever the live API currently exposes, including valid variants.
2. Fetch data for a polygon: `fetch_all_pages` (or `post_large_polygon` for
   polygons too big to fit a URL). Both can return data directly, or —
   with respond_async=True — a `task_id` for a big/slow query.
3. Look up specific records: `get_object_by_id`, `post_objects_by_ids`
   (batch), or `get_objects_metadata` (field schema for an entity).
4. Chain a concept onto fetches: GET /concept/* tools combine results via
   their task_ids (task_id_primary_entity / task_id_secondary_entity) and
   wait for those inputs themselves; POST /concept/* tools take GeoJSON.
5. Any tool result containing a bare `task_id` needs `wait_for_task` (or
   `manage_task`) before its data is usable — poll, don't guess. Several
   tasks? `wait_for_tasks` resolves them all in one call.

Independent calls run concurrently: issue them in the same turn (several
fetches, several concept submits) instead of one after another.

Every tool error ends with a hint and [retryable=yes|no]; follow the hint.
Only want counts/stats, not geometries? `manage_task(view="describe")`
instead of pulling the full FeatureCollection into context.
"""


def _summarize_wkt(wkt: Any) -> Any:
    """Replace a WKT AOI with its bbox: a real subscription polygon is ~180 KB of text."""
    if not isinstance(wkt, str):
        return wkt
    summary: dict[str, Any] = {"wkt_chars": len(wkt)}
    if re.match(r"\s*\w+\s*(Z|M|ZM)\b", wkt, re.IGNORECASE):
        return summary  # 3-/4-D coordinates: pairing x/y by position would be wrong
    nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", wkt)]
    xs, ys = nums[0::2], nums[1::2]
    if xs and ys:
        summary["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
    return summary


def _bounded(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Keep a FeatureCollection under max_bytes by dropping trailing features.

    Trimming whole features keeps the result valid JSON (a byte cap on the
    serialized text cuts mid-string) and says how much was left out.
    """
    features = result.get("features")
    if max_bytes <= 0 or not isinstance(features, list):
        return result
    if len(json.dumps(result)) <= max_bytes:
        return result
    budget = max_bytes - len(json.dumps({**result, "features": []})) - 200
    kept: list[Any] = []
    for feature in features:
        budget -= len(json.dumps(feature)) + 1
        if budget < 0:
            break
        kept.append(feature)
    return {
        **result,
        "features": kept,
        "truncated": True,
        "total_features": result.get("total_features", len(features)),
        "hint": "Result exceeded the size cap. Use respond_async=True then "
        "manage_task(view='describe'), or geo_type='centroid' / simplify / filter_string.",
    }


class _AwaitInputTasks(Middleware):
    """Wait for a concept's input tasks before forwarding the call.

    The API 409s a GET /concept/* whose task_id_* input is still queued or
    running, so a fetch → buffer → sjoin chain fired back-to-back fails on
    the second hop unless the caller waits in between. Inputs are awaited
    together under one deadline. Applies only to the tools that take
    task_id_* inputs, so other calls pay nothing.
    """

    def __init__(
        self, client: httpx.AsyncClient, tools: frozenset[str], max_wait: float = 120.0
    ) -> None:
        self._client = client
        self._tools = tools
        self._max_wait = max_wait

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        if context.message.name in self._tools:
            args = context.message.arguments or {}
            inputs = [(k, args[k]) for k in TASK_INPUT_ARGS if isinstance(args.get(k), str)]

            async def wait(key: str, task_id: str) -> None:
                try:
                    await poll_until_done(self._client, task_id, self._max_wait)
                except (TaskFailed, TimeoutError, httpx.HTTPError) as exc:
                    raise ToolError(f"{key}={task_id}: {exc}") from exc

            await asyncio.gather(*(wait(k, t) for k, t in inputs))
        return await call_next(context)


class _TagErrors(Middleware):
    """Append the recovery hint and [retryable=...] to every tool error, generated ones included."""

    def __init__(self, write_tools: frozenset[str]) -> None:
        self._write_tools = write_tools

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except Exception as exc:
            read_only = context.message.name not in self._write_tools
            raise ToolError(tag_error(str(exc), read_only)) from exc


def build_server(base_url: str, username: str, password: str) -> FastMCP:
    spec = load_spec(base_url)
    client = make_client(base_url, username, password)
    source_registry, source_variants, list_paths = build_source_registry(spec)
    max_bytes = int(
        os.environ.get("SIGNALEYES_MAX_TOOL_RESPONSE_BYTES", _DEFAULT_MAX_RESPONSE_BYTES)
    )

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="SignalEyes API",
        route_maps=build_route_maps(source_registry, list_paths),
        instructions=SERVER_INSTRUCTIONS,
        mcp_names=MCP_NAMES,
        mcp_component_fn=annotate_openapi_component,
    )
    mcp.add_provider(SkillProvider(_SKILLS_DIR / "signaleyes"))
    write_tools = write_tool_names(spec) | {"post_large_polygon", "post_objects_by_ids"}
    mcp.add_middleware(_TagErrors(write_tools))
    mcp.add_middleware(_AwaitInputTasks(client, task_input_tools(spec)))

    @mcp.tool(annotations=READ_OPEN)
    async def wait_for_task(
        task_id: str,
        max_wait: float = 120.0,
        poll_interval: float = 2.0,
    ) -> dict[str, Any]:
        """Poll a SignalEyes background task until it finishes, then return its result.

        Many endpoints (concepts, analyses, and source fetches sent with the
        `Prefer: respond-async` header) return a task_id immediately instead
        of a result. This collapses the manual GET /task/{id} polling loop
        into one call. Polling starts at 0.3s and backs off to
        `poll_interval`. A failed task raises with its error and is left
        for `delete_task`; a task still running at `max_wait` raises and
        can be waited on again.
        """
        result = await poll_until_done(client, task_id, max_wait, poll_interval, fetch_result=True)
        assert result is not None
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=READ_OPEN)
    async def wait_for_tasks(
        task_ids: list[str],
        view: Literal["describe", "result"] = "describe",
        max_wait: float = 120.0,
    ) -> dict[str, Any]:
        """Wait for several tasks at once and return each one's describe (default) or result.

        One call instead of one `wait_for_task` per task: the waits run
        concurrently server-side, so N tasks take as long as the slowest,
        not the sum. Returns {task_id: ...}; a task that fails or times
        out yields {"error": ...} under its id without sinking the others.
        With view="result" the size cap is shared across the batch, so
        prefer "describe" unless you need geometries.
        """
        per_task_bytes = max_bytes // max(len(task_ids), 1)

        async def one(task_id: str) -> dict[str, Any]:
            try:
                if view == "result":
                    result = await poll_until_done(client, task_id, max_wait, fetch_result=True)
                    assert result is not None
                    return _bounded(result, per_task_bytes)
                await poll_until_done(client, task_id, max_wait)
                resp = await client.get(f"/task/{task_id}/describe")
                check_response(resp)
                described: dict[str, Any] = resp.json()
                return described
            except (TaskFailed, TimeoutError, httpx.HTTPError) as exc:
                return {"error": str(exc)}

        results = await asyncio.gather(*(one(t) for t in task_ids))
        return dict(zip(task_ids, results))

    @mcp.tool(annotations=READ_OPEN)
    async def manage_task(
        task_id: str, view: Literal["status", "result", "describe", "metadata"] = "status"
    ) -> dict[str, Any]:
        """Inspect a background task without deleting it.

        view:
          - "status" (default) — id/status/error, also for a failed task.
            If still running, use `wait_for_task` instead of polling by hand.
          - "result" — the completed task's data (409 if not done yet).
          - "describe" — a statistical summary of the result (feature
            counts, attributes) without pulling the full result into context.
          - "metadata" — how the task was created (zoom, source entity,
            geometry type, CRS, creation time).

        Deletion is intentionally not a `view` here and has its own
        `delete_task` tool instead — an MCP client that approves reads on
        this tool shouldn't thereby approve deleting the task too.
        """
        if view == "status":
            return await task_status(client, task_id)
        resp = await client.get(f"/task/{task_id}/{view}")
        check_response(resp)
        result: dict[str, Any] = resp.json()
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=READ_LOCAL)
    def list_source_entities() -> dict[str, dict[str, Any]]:
        """List every /source/{entity} dataset and which operations it supports.

        Start here when unsure which `entity`/`variant` to pass to
        `fetch_all_pages`, `post_large_polygon`, `get_object_by_id`,
        `post_objects_by_ids`, or `get_objects_metadata` — this reflects
        whatever the live API currently exposes, not a fixed list.

        Returns, per entity, which of "list" (polygon query), "by_id",
        "metadata", "batch" it supports, and whether that operation needs a
        `variant` sub-type ("no variant" / "variant required" / "variant
        optional" — optional means both a bare and a variant form exist),
        plus "variants": the valid values where the API declares them.
        """
        return describe_source_registry(source_registry, source_variants)

    @mcp.tool(annotations=READ_OPEN)
    async def get_object_by_id(
        entity: str, object_id: str, variant: str | None = None
    ) -> dict[str, Any]:
        """Fetch one /source/{entity} object by id (a FeatureCollection with one feature).

        See `list_source_entities` for which entities support this and
        whether `variant` is required (e.g. entity="building" needs a
        variant like "detail"; entity="water" takes none at all).
        """
        path = resolve_source_path(source_registry, entity, "by_id", variant, id=object_id)
        resp = await client.get(path)
        check_response(resp)
        result: dict[str, Any] = resp.json()
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=READ_OPEN)
    async def post_objects_by_ids(
        entity: str, ids: list[str], variant: str | None = None
    ) -> dict[str, Any]:
        """Batch-fetch up to 100 /source/{entity} objects by id in one call.

        All ids must fall within the caller's area of interest — if any are
        outside it or don't exist, the whole request fails listing the bad
        ids. See `list_source_entities` for which entities support batch
        lookup and whether they need `variant`.
        """
        path = resolve_source_path(source_registry, entity, "batch", variant)
        resp = await client.post(path, json={"ids": ids})
        check_response(resp)
        result: dict[str, Any] = resp.json()
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=READ_OPEN)
    async def get_objects_metadata(entity: str, variant: str | None = None) -> dict[str, Any]:
        """Fetch the field/schema metadata for a /source/{entity} dataset.

        Check this before querying an unfamiliar entity, to see its real
        attribute names instead of guessing what goes in `filter_string`.
        See `list_source_entities` for which entities need `variant`.
        """
        path = resolve_source_path(source_registry, entity, "metadata", variant)
        resp = await client.get(path)
        check_response(resp)
        result: dict[str, Any] = resp.json()
        return result

    @mcp.tool(annotations=READ_OPEN)
    async def fetch_all_pages(
        entity: str,
        polygon: str,
        variant: str | None = None,
        max_pages: int = 10,
        filter_string: str | None = None,
        geo_type: str | None = None,
        simplify: float | None = None,
        sample: float | None = None,
        dissolve: bool | None = None,
        intersect_centroid: bool | None = None,
        attributes: str | None = None,
        groupby: str | None = None,
        respond_async: bool = False,
    ) -> dict[str, Any]:
        """Fetch every page of a GET /source/{entity} query and merge into one FeatureCollection.

        The API paginates via the X-Total-Pages response header, which the
        generated per-endpoint tools can't see — this does the paging in one
        call, with the same filters as the API's single-page query
        (filter_string, geo_type, simplify, sample, dissolve,
        intersect_centroid, attributes/groupby — the last two only apply to
        entity="thirdparty").

        Paging stops at `max_pages` (default 10) or once the accumulated
        response passes the size cap, whichever comes first, so an
        unexpectedly large area doesn't flood your context with features —
        raise `max_pages` once you know the area is small, or set
        respond_async=True to have the API gather every page itself and
        hand back a `task_id` instead (feed that to `wait_for_task`).

        Only want a count or attribute summary, not the features themselves?
        Call this with respond_async=True, then
        `manage_task(task_id, view="describe")` instead of `wait_for_task`:
        feature counts and per-attribute stats without the geometries.

        See `list_source_entities` for valid `entity`/`variant` combinations.
        """
        path = resolve_source_path(source_registry, entity, "list", variant)
        extra_params = _params(
            filter_string=filter_string,
            geo_type=geo_type,
            simplify=simplify,
            sample=sample,
            dissolve=dissolve,
            intersect_centroid=intersect_centroid,
            attributes=attributes,
            groupby=groupby,
        )

        if respond_async:
            resp = await client.get(
                path,
                params={"polygon": polygon, **extra_params},
                headers={"Prefer": "respond-async"},
            )
            check_response(resp)
            async_result: dict[str, Any] = resp.json()
            return async_result

        features: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        size = 0
        while page <= min(total_pages, max_pages) and (max_bytes <= 0 or size < max_bytes):
            resp = await client.get(path, params={"polygon": polygon, "page": page, **extra_params})
            check_response(resp)
            total_pages = int(resp.headers.get("X-Total-Pages", 1))
            features.extend(resp.json().get("features", []))
            size += len(resp.content)
            page += 1
        result: dict[str, Any] = {
            "type": "FeatureCollection",
            "features": features,
            "total_pages": total_pages,
            "pages_fetched": page - 1,
        }
        if page - 1 < total_pages:
            # The real total is unknown without the remaining pages.
            result["truncated"] = True
            result["hint"] = (
                "Not all pages fetched. For the full count use respond_async=True then "
                "manage_task(view='describe'); for the data raise max_pages or narrow the area."
            )
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=WRITE_OPEN)
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

        Replaces the generated post_* source tools, which always fail: the
        API declares these request bodies as a bare JSON string (just the
        WKT polygon), and FastMCP sends bare-string bodies as raw bytes with
        no Content-Type, so the API rejects every call with a JSON decode
        error. This tool serializes the body correctly.

        Set respond_async=True for large AOIs — the API replies with a task
        ID immediately instead of gathering every page inline; resolve it
        with wait_for_task, or `manage_task(task_id, view="describe")` for
        counts and stats only.

        Like the GET endpoint, results are paginated; the response carries
        `total_pages`/`page` (from the X-Total-Pages/X-Page-Number headers)
        so the caller knows whether to fetch more with `page=2`, etc.
        See `list_source_entities` for valid `entity`/`variant` combinations.
        """
        path = resolve_source_path(source_registry, entity, "list", variant)
        params = _params(
            filter_string=filter_string,
            page=page,
            geo_type=geo_type,
            simplify=simplify,
            sample=sample,
            dissolve=dissolve,
            intersect_centroid=intersect_centroid,
        )
        headers = {"Prefer": "respond-async"} if respond_async else {}

        resp = await client.post(path, params=params, json=polygon, headers=headers)
        check_response(resp)
        result: dict[str, Any] = resp.json()
        if "X-Total-Pages" in resp.headers:
            result["total_pages"] = int(resp.headers["X-Total-Pages"])
            result["page"] = int(resp.headers.get("X-Page-Number", page))
        return _bounded(result, max_bytes)

    @mcp.tool(annotations=READ_OPEN)
    async def whoami() -> dict[str, Any]:
        """Check SignalEyes credentials by fetching the caller's active subscriptions."""
        resp = await client.get("/subscriptions")
        check_response(resp)
        body: dict[str, Any] = resp.json()
        for sub in body.get("subscriptions", []):
            sub["aoi"] = _summarize_wkt(sub.get("aoi"))
        return body

    return mcp


def _params(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


async def run_smoke(
    base_url: str,
    username: str,
    password: str,
    entity: str | None = None,
    polygon: str = SMOKE_POLYGON,
) -> int:
    """Run one real query against SignalEyes: checks credentials and connectivity end to end."""
    entity = entity or random.choice(SMOKE_ENTITIES)
    async with make_client(base_url, username, password) as client:
        who_resp = await client.get("/subscriptions")
        check_response(who_resp)
        print(f"Credentials OK. Subscriptions: {who_resp.json()}")

        query_resp = await client.get(f"/source/{entity}", params={"polygon": polygon, "page": 1})
        check_response(query_resp)
        data = query_resp.json()
        total_pages = query_resp.headers.get("X-Total-Pages", "1")
        print(
            f"Sample query GET /source/{entity}: "
            f"{len(data.get('features', []))} feature(s) on page 1 of {total_pages}"
        )
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signaleyes-mcp")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the MCP server (stdio default, or streamable HTTP)")
    serve.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for a single MCP client; http for a shared orchestrator endpoint",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (http transport only)")
    serve.add_argument("--port", type=int, default=8000, help="Bind port (http transport only)")
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
    transport = getattr(args, "transport", "stdio")
    if transport == "http":
        # Stateless: no session affinity, so concurrent orchestrator workers can share one process.
        server.run(transport="http", host=args.host, port=args.port, stateless_http=True)
    else:
        server.run()


if __name__ == "__main__":  # pragma: no cover
    main()
