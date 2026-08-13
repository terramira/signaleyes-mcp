"""MCP server exposing NEO's SignalEyes API (SuperAPI) as tools.

Turns https://api.neo.nl/v1's OpenAPI spec into MCP tools via
FastMCP.from_openapi. Most operations still get a tool generated
automatically, but hand-written tools cover two kinds of gap: things the
spec can't express at all (task polling, header-driven pagination,
credential check), and — since the API's ~115 operations are mostly the same
four shapes (query, id lookup, batch id lookup, metadata) repeated per
source entity — the repeated shapes, which collapse into a handful of
generic `entity`-parameterized tools instead of one MCP tool per entity. The
entity/operation support matrix behind those generic tools is derived from
the live spec at startup, not hand-typed, so it can't drift from what the
API actually exposes.

`uv run signaleyes-mcp` also supports two non-serving subcommands:
`test` (runs the test suite) and `smoke` (runs one real query against the
live API to confirm credentials/connectivity work).
"""

import argparse
import asyncio
import os
import random
import re
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
# the usage-records XLSX export. /subscriptions and /token/* are excluded
# for a different reason: /subscriptions just duplicates `whoami`, and the
# /token/* endpoints issue OAuth2 bearer tokens this server never uses (the
# shared httpx client always signs requests with HTTP Basic auth instead) —
# calling them would hand the caller a token nothing else here can use.
# Everything else becomes a tool.
EXCLUDED_PATTERNS = (
    r"^/source/wfs/.*",
    r"^/source/tiles/.*",
    r"^/source/(dsm|dtm)(/pages)?(/\{quadkey\})?$",
    r"^/analyses/shadow_modelling$",
    r"^/usage/records$",
    r"^/subscriptions$",
    r"^/token/(generate|refresh|revoke)$",
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

# Path parameter names that mean "sub-type of this entity" (e.g. a building
# category, a landscape-element type, a third-party dataset identifier) as
# opposed to a record id. Used to tell a /source/{entity} "list" route from
# a /source/{entity}/{id} "single record" route while deriving the registry.
_VARIANT_PARAM_NAMES = ("entity", "identifier")

# The concept/analyses/task/geojson operations stay auto-generated (see the
# module docstring for why), but FastMCP's default tool name is the raw
# OpenAPI operationId — FastAPI's auto-generated ones, in particular, repeat
# the function name and path (e.g. "get_task_concept_overlay_concept_overlay_get").
# Every session that connects pays for that length when tools are listed.
# Mapped here to short, stable names instead; keyed by the exact operationId
# so a wrong/renamed key just silently falls back to the default rather than
# erroring — verify against a fresh openapi.json if a name below goes missing.
# Where an operation has both a GET (task_id-chaining) and POST (live-entity)
# form, the GET one — the one this server's skill recommends by default —
# keeps the plain name; POST gets a "_from_entity" suffix.
MCP_NAMES = {
    "run_analyses_3_30_300_analyses_3_30_300_get": "analyses_3_30_300",
    "run_analyses_baf_analyses_baf__get": "analyses_baf",
    "run_concept_aggregation_concept_aggregation_post": "concept_aggregation",
    "get_single_task_concept_buffer_concept_buffer_get": "concept_buffer",
    "get_task_concept_clip_concept_clip_get": "concept_clip",
    "get_task_concept_closest_concept_closest_get": "concept_closest",
    "run_concept_closest_concept_closest_post": "concept_closest_from_entity",
    "get_task_concept_density_area_concept_density_area_get": "concept_density_area",
    "run_concept_density_area_concept_density_area_post": "concept_density_area_from_entity",
    "get_task_concept_density_count_concept_density_count_get": "concept_density_count",
    "run_concept_density_count_concept_density_count_post": "concept_density_count_from_entity",
    "run_concept_difference_area_concept_difference_area_post": "concept_difference_area",
    "get_task_concept_overlay_concept_overlay_get": "concept_overlay",
    "get_task_concept_sjoin_concept_sjoin_get": "concept_sjoin",
    "get_task_concept_sjoin_nearest_concept_sjoin_nearest_get": "concept_sjoin_nearest",
    "run_concept_shadow_modelling_concept_shadow_modelling_post": "concept_shadow_modelling",
    "get_usages_summary_usage_summary_get": "usage_summary",
    "post_geojson_source_geojson_source_geojson_post": "post_geojson",
    "delete_task_task__task_id__delete": "delete_task",
}

SourceRegistry = dict[str, dict[str, dict[bool, str]]]

SERVER_INSTRUCTIONS = """\
SignalEyes: geospatial data (buildings, trees, roads, water, solar panels, \
...) plus chainable analytical concepts (density, closest, buffer, overlay, \
sjoin, ...) over an area-of-interest polygon.

Typical flow:
1. Unsure which dataset/entity to use? Call `list_source_entities` — it
   reflects whatever the live API currently exposes.
2. Fetch data for a polygon: `fetch_all_pages` (or `post_large_polygon` for
   polygons too big to fit a URL). Both can return data directly, or —
   with respond_async=True — a `task_id` for a big/slow query.
3. Look up specific records: `get_object_by_id`, `post_objects_by_ids`
   (batch), or `get_objects_metadata` (field schema for an entity).
4. Chain a concept onto completed fetches: POST /concept/* tools take an
   entity + params and return a task_id; GET /concept/* tools combine two
   already-fetched results via their task_ids (task_id_primary_entity /
   task_id_secondary_entity).
5. Any tool result containing a bare `task_id` needs `wait_for_task` (or
   `manage_task`) before its data is usable — poll, don't guess.

If a call fails with 401/403, run `whoami` first to confirm credentials.
"""


def _make_client(base_url: str, username: str, password: str) -> httpx.AsyncClient:
    # httpx's default timeout (5s across connect/read/write/pool) is too
    # tight for this API: task-status polls, /result and /describe on large
    # datasets, and delete_task have all been observed taking longer than
    # 5s under normal (non-degraded) conditions. A request that merely runs
    # long — not stuck — then raises httpx.TimeoutException, which every
    # tool sharing this client (auto-generated ones included) surfaces as a
    # bare "Upstream request timed out, please retry", even when the
    # underlying task had already finished. 10s to establish a connection,
    # 60s to receive a response, is generous without hanging indefinitely
    # on a genuinely dead connection.
    timeout = httpx.Timeout(10.0, read=60.0)
    return httpx.AsyncClient(
        base_url=base_url, auth=httpx.BasicAuth(username, password), timeout=timeout
    )


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


def _is_excluded(path: str) -> bool:
    return any(re.match(p, path) for p in EXCLUDED_PATTERNS)


def _build_source_registry(spec: dict[str, Any]) -> tuple[SourceRegistry, list[str]]:
    """Derive the /source/{entity} operation support matrix from the live spec.

    Almost every /source/* path is one of four shapes: "list" (the polygon
    query), "by_id" (.../{id}), "metadata" (.../metadata), or "batch"
    (POST .../batch, id lookup). Some entities need a `variant` path segment
    (e.g. /source/building/{entity}/...) and some don't, and which shapes
    exist at all varies per entity (bag/district/neighborhood have no by_id
    or batch; thirdparty has no bare list form; building/tree support both a
    bare list and a variant one). Reading this straight from `spec["paths"]`
    instead of hand-typing it means the generic tools below track whatever
    the API currently exposes, the same guarantee the rest of this
    auto-generated server already has.

    Returns the registry (entity -> shape -> {variant_required: path
    template, using the placeholder "{variant}"/"{id}"}), plus the literal
    (non-normalized) list of GET "list" paths — used to exclude the
    single-page auto-generated tools that `fetch_all_pages` already covers.
    """
    registry: SourceRegistry = {}
    list_get_paths: list[str] = []
    for path, path_item in spec.get("paths", {}).items():
        if not path.startswith("/source/") or _is_excluded(path):
            continue
        segs = path.split("/")
        last = segs[-1]
        for method in path_item:
            if method not in ("get", "post"):
                continue
            if last == "metadata":
                shape = "metadata"
            elif last == "batch" and method == "post":
                shape = "batch"
            elif (
                last.startswith("{")
                and last.strip("{}") not in _VARIANT_PARAM_NAMES
                and method == "get"
            ):
                shape = "by_id"
            elif method == "get":
                shape = "list"
                list_get_paths.append(path)
            else:
                continue

            norm = ["{variant}" if s.strip("{}") in _VARIANT_PARAM_NAMES else s for s in segs]
            entity_segs = norm[2:] if shape == "list" else norm[2:-1]
            entity_key = "_".join(s for s in entity_segs if s != "{variant}").replace("-", "_")
            requires_variant = "{variant}" in norm
            registry.setdefault(entity_key, {}).setdefault(shape, {})[requires_variant] = "/".join(
                norm
            )
    return registry, list_get_paths


def _route_pattern(template: str) -> str:
    """Turn a normalized registry template back into a RouteMap-matchable regex.

    RouteMap.pattern matches against the spec's literal path template (e.g.
    "/source/building/{entity}/{id}"), not a resolved URL — so the
    normalized "{variant}" placeholder has to become a regex alternation
    over the real param names it could stand for ({entity}/{identifier})
    rather than being treated as literal text.
    """
    parts = [
        r"\{(?:entity|identifier)\}" if seg == "{variant}" else re.escape(seg)
        for seg in template.split("/")
    ]
    return "^" + "/".join(parts) + "$"


def _resolve_source_path(
    registry: SourceRegistry, entity: str, shape: str, variant: str | None, **path_kwargs: str
) -> str:
    templates = registry.get(entity, {}).get(shape)
    if not templates:
        supported = sorted(e for e, shapes in registry.items() if shape in shapes)
        raise ValueError(f"{entity!r} has no {shape} endpoint. Entities that do: {supported}")
    has_variant = variant is not None
    if has_variant not in templates:
        if has_variant:
            raise ValueError(f"{entity!r}'s {shape} endpoint doesn't take a variant.")
        raise ValueError(
            f"{entity!r}'s {shape} endpoint requires a variant sub-type "
            "(check get_objects_metadata or list_source_entities)."
        )
    return templates[has_variant].format(variant=variant, **path_kwargs)


def _describe_source_registry(registry: SourceRegistry) -> dict[str, dict[str, str]]:
    described: dict[str, dict[str, str]] = {}
    for entity, shapes in sorted(registry.items()):
        entry: dict[str, str] = {}
        for shape in ("list", "by_id", "metadata", "batch"):
            variants = shapes.get(shape)
            if variants is None:
                continue
            if True in variants and False in variants:
                entry[shape] = "variant optional"
            elif True in variants:
                entry[shape] = "variant required"
            else:
                entry[shape] = "no variant"
        described[entity] = entry
    return described


def build_server(base_url: str, username: str, password: str) -> FastMCP:
    resp = httpx.get(f"{base_url}/openapi.json", timeout=30)
    resp.raise_for_status()
    spec = resp.json()

    client = _make_client(base_url, username, password)
    source_registry, redundant_list_paths = _build_source_registry(spec)

    route_maps = [RouteMap(pattern=p, mcp_type=MCPType.EXCLUDE) for p in EXCLUDED_PATTERNS]
    route_maps.extend(
        RouteMap(pattern=p, methods=["POST"], mcp_type=MCPType.EXCLUDE)
        for p in _BROKEN_POLYGON_BODY_POST_PATTERNS
    )
    # /source/{entity}/{id}, /metadata, and POST .../batch for every entity
    # collapse into get_object_by_id / get_objects_metadata /
    # post_objects_by_ids below; the single-page GET query for every entity
    # collapses into fetch_all_pages (which already covers it, page cap and
    # all). Templates from the registry use the normalized "{variant}"
    # placeholder, so route_pattern() maps it back to whichever real path
    # param name ({entity} or {identifier}) the spec actually uses.
    route_maps.extend(
        RouteMap(pattern=rf"^{re.escape(p)}$", methods=["GET"], mcp_type=MCPType.EXCLUDE)
        for p in redundant_list_paths
    )
    route_maps.extend(
        RouteMap(pattern=_route_pattern(p), mcp_type=MCPType.EXCLUDE)
        for shapes in source_registry.values()
        for shape, variants in shapes.items()
        if shape in ("by_id", "metadata", "batch")
        for p in variants.values()
    )
    # GET-only: /task/{task_id}'s DELETE method (delete_task) stays its own
    # tool — see manage_task's docstring for why deletion isn't folded in.
    route_maps.extend(
        RouteMap(pattern=p, methods=["GET"], mcp_type=MCPType.EXCLUDE)
        for p in (
            r"^/task/\{task_id\}$",
            r"^/task/\{task_id\}/describe$",
            r"^/task/\{task_id\}/metadata$",
            r"^/task/\{task_id\}/result$",
        )
    )
    route_maps.append(RouteMap(methods=["GET", "POST", "DELETE"], mcp_type=MCPType.TOOL))

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="SignalEyes API",
        route_maps=route_maps,
        instructions=SERVER_INSTRUCTIONS,
        mcp_names=MCP_NAMES,
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

        A single slow request during polling doesn't fail the whole call —
        it's retried on the next poll_interval tick, still bounded by
        max_wait — since a transient timeout on one poll says nothing about
        whether the task itself succeeded.
        """
        try:
            async with asyncio.timeout(max_wait):
                while True:
                    try:
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
                    except httpx.TimeoutException:
                        pass
                    await asyncio.sleep(poll_interval)
        except TimeoutError as exc:
            raise TimeoutError(f"Task {task_id} did not complete within {max_wait}s") from exc

    @mcp.tool
    async def manage_task(task_id: str, view: str = "status") -> dict[str, Any]:
        """Inspect a background task without deleting it.

        view:
          - "status" (default) — id/status/error. If still running, use
            `wait_for_task` instead of polling this by hand.
          - "result" — the completed task's data (409 if not done yet).
          - "describe" — a statistical summary of the result (feature
            counts, attributes) without pulling the full result into context.
          - "metadata" — how the task was created (zoom, source entity,
            geometry type, CRS, creation time).

        Deletion is intentionally not a `view` here and has its own
        `delete_task` tool instead — an MCP client that approves reads on
        this tool shouldn't thereby approve deleting the task too.
        """
        views = {
            "status": "",
            "describe": "/describe",
            "metadata": "/metadata",
            "result": "/result",
        }
        if view not in views:
            raise ValueError(f"view must be one of {sorted(views)}, got {view!r}")
        resp = await client.get(f"/task/{task_id}{views[view]}")
        _check_response(resp)
        result: dict[str, Any] = resp.json()
        return result

    @mcp.tool
    def list_source_entities() -> dict[str, dict[str, str]]:
        """List every /source/{entity} dataset and which operations it supports.

        Start here when unsure which `entity`/`variant` to pass to
        `fetch_all_pages`, `post_large_polygon`, `get_object_by_id`,
        `post_objects_by_ids`, or `get_objects_metadata` — this reflects
        whatever the live API currently exposes, not a fixed list.

        Returns, per entity, which of "list" (polygon query), "by_id",
        "metadata", "batch" it supports, and whether that operation needs a
        `variant` sub-type ("no variant" / "variant required" / "variant
        optional" — optional means both a bare and a variant form exist).
        """
        return _describe_source_registry(source_registry)

    @mcp.tool
    async def get_object_by_id(
        entity: str, object_id: str, variant: str | None = None
    ) -> dict[str, Any]:
        """Fetch one /source/{entity} object by id (a FeatureCollection with one feature).

        See `list_source_entities` for which entities support this and
        whether `variant` is required (e.g. entity="building" needs a
        variant like "school"; entity="water" takes none at all).
        """
        path = _resolve_source_path(source_registry, entity, "by_id", variant, id=object_id)
        resp = await client.get(path)
        _check_response(resp)
        result: dict[str, Any] = resp.json()
        return result

    @mcp.tool
    async def post_objects_by_ids(
        entity: str, ids: list[str], variant: str | None = None
    ) -> dict[str, Any]:
        """Batch-fetch up to 100 /source/{entity} objects by id in one call.

        All ids must fall within the caller's area of interest — if any are
        outside it or don't exist, the whole request fails listing the bad
        ids. See `list_source_entities` for which entities support batch
        lookup and whether they need `variant`.
        """
        path = _resolve_source_path(source_registry, entity, "batch", variant)
        resp = await client.post(path, json={"ids": ids})
        _check_response(resp)
        result: dict[str, Any] = resp.json()
        return result

    @mcp.tool
    async def get_objects_metadata(entity: str, variant: str | None = None) -> dict[str, Any]:
        """Fetch the field/schema metadata for a /source/{entity} dataset.

        Check this before querying an unfamiliar entity, to see its real
        attribute names instead of guessing what goes in `filter_string`.
        See `list_source_entities` for which entities need `variant`.
        """
        path = _resolve_source_path(source_registry, entity, "metadata", variant)
        resp = await client.get(path)
        _check_response(resp)
        result: dict[str, Any] = resp.json()
        return result

    @mcp.tool
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
        auto-generated per-endpoint tools can't see (they only surface the
        JSON body) — so fetching a multi-page area otherwise takes one tool
        call per page, driven by trial and error. This does it in one call,
        while still supporting the same filters as the API's single-page
        query (filter_string, geo_type, simplify, sample, dissolve,
        intersect_centroid, attributes/groupby — the last two only apply to
        entity="thirdparty").

        `max_pages` defaults low (10, not "all") so an unexpectedly large
        area doesn't flood your context with features — raise it
        explicitly once you know the area is small, or set
        respond_async=True to have the API gather every page itself and
        hand back a `task_id` instead (feed that to `wait_for_task`).

        Only want a count or attribute summary, not the features themselves?
        Don't pull the full result to count it client-side — call this with
        respond_async=True, then `manage_task(task_id, view="describe")`
        instead of `wait_for_task`. describe returns feature counts and
        per-attribute stats without the geometries, which for a query
        returning thousands of features is the difference between a few KB
        and a multi-MB result landing in context.

        See `list_source_entities` for valid `entity`/`variant` combinations.
        """
        path = f"/source/{entity}/{variant}" if variant else f"/source/{entity}"
        extra_params: dict[str, Any] = {
            "filter_string": filter_string,
            "geo_type": geo_type,
            "simplify": simplify,
            "sample": sample,
            "dissolve": dissolve,
            "intersect_centroid": intersect_centroid,
            "attributes": attributes,
            "groupby": groupby,
        }
        extra_params = {k: v for k, v in extra_params.items() if v is not None}

        if respond_async:
            resp = await client.get(
                path,
                params={"polygon": polygon, **extra_params},
                headers={"Prefer": "respond-async"},
            )
            _check_response(resp)
            async_result: dict[str, Any] = resp.json()
            return async_result

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
        with wait_for_task. Only want a count or attribute summary? Use
        `manage_task(task_id, view="describe")` instead of wait_for_task —
        see fetch_all_pages' docstring for why.

        Like the GET endpoint, results are paginated; the response carries
        `total_pages`/`page` (from the X-Total-Pages/X-Page-Number headers)
        so the caller knows whether to fetch more with `page=2`, etc.
        See `list_source_entities` for valid `entity`/`variant` combinations.
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
