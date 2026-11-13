"""OpenAPI spec handling: loading/caching, what to expose, and the source-entity registry.

The API's ~115 operations are mostly four shapes (query, id lookup, batch id
lookup, metadata) repeated per source entity. The registry derived here from
the live spec lets a handful of generic `entity`-parameterised tools replace
one generated tool per entity, and tracks whatever the API currently exposes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx2
from fastmcp.server.providers.openapi import MCPType, OpenAPITool, RouteMap
from fastmcp.utilities.openapi import HTTPRoute
from mcp.types import ToolAnnotations

# Binary/XML endpoints an LLM tool call can't usefully consume: raw OGC WFS,
# vector tiles, DSM/DTM raster tiles (their openapi.json entry claims
# application/json, but they actually return image/tiff — confirmed by
# direct request), the shadow-modelling raster, the usage-records XLSX
# export, and the Apache Arrow IPC form of the thirdparty query.
# /subscriptions is excluded because it just duplicates `whoami`, and
# /token/* because it issues OAuth2 bearer tokens this server never uses
# (the shared httpx client signs requests with HTTP Basic auth instead).
EXCLUDED_PATTERNS = (
    r"^/source/wfs/.*",
    r"^/source/tiles/.*",
    r"^/source/(dsm|dtm)(/pages)?(/\{quadkey\})?$",
    r"^/source/thirdparty/\{identifier\}/arrow$",
    r"^/analyses/shadow_modelling$",
    r"^/usage/records$",
    r"^/subscriptions$",
    r"^/token/(generate|refresh|revoke)$",
)

# closest/density_area/density_count exist as both GET (chain a task_id) and
# POST (post live GeoJSON). The skill tells the model to use the GET form and
# the POST form's schema is 2-3k chars each, paid on every session's tool
# listing — so the POST form is excluded rather than renamed.
DUPLICATE_POST_CONCEPT_PATTERN = r"^/concept/(closest|density_area|density_count)$"

# Upstream descriptions end with a long "how to paginate via response
# headers" recipe. The generated tools can't see headers, and
# fetch_all_pages already handles paging, so it's dead weight on every
# session's tool listing.
PAGINATION_BOILERPLATE = "The data is delivered in pages."

# Path parameter names that mean "sub-type of this entity" (a building
# category, a landscape-element type, a third-party dataset identifier) as
# opposed to a record id.
VARIANT_PARAM_NAMES = ("entity", "identifier")

# FastMCP's default tool name is the raw OpenAPI operationId, and FastAPI's
# repeat the function name and path ("get_task_concept_overlay_concept_overlay_get").
# Every session pays for that length when tools are listed. Keyed by the exact
# operationId, so a renamed key silently falls back to the default — verify
# against a fresh openapi.json if a short name below goes missing.
MCP_NAMES = {
    "run_analyses_3_30_300_analyses_3_30_300_get": "analyses_3_30_300",
    "run_analyses_baf_analyses_baf__get": "analyses_baf",
    "run_concept_aggregation_concept_aggregation_post": "concept_aggregation",
    "get_single_task_concept_buffer_concept_buffer_get": "concept_buffer",
    "get_single_task_concept_centerline_concept_centerline_get": "concept_centerline",
    "get_single_task_concept_centerpoint_concept_centerpoint_get": "concept_centerpoint",
    "get_task_concept_clip_concept_clip_get": "concept_clip",
    "get_task_concept_closest_concept_closest_get": "concept_closest",
    "get_task_concept_density_area_concept_density_area_get": "concept_density_area",
    "get_task_concept_density_count_concept_density_count_get": "concept_density_count",
    "run_concept_difference_area_concept_difference_area_post": "concept_difference_area",
    "get_task_concept_overlay_concept_overlay_get": "concept_overlay",
    "get_task_concept_sjoin_concept_sjoin_get": "concept_sjoin",
    "get_task_concept_sjoin_nearest_concept_sjoin_nearest_get": "concept_sjoin_nearest",
    "run_concept_shadow_modelling_concept_shadow_modelling_post": "concept_shadow_modelling",
    "get_usages_summary_usage_summary_get": "usage_summary",
    "post_geojson_source_geojson_source_geojson_post": "post_geojson",
    "delete_task_task__task_id__delete": "delete_task",
}

TASK_INPUT_ARGS = ("task_id_primary_entity", "task_id_secondary_entity")

SourceRegistry = dict[str, dict[str, dict[bool, str]]]
SourceVariants = dict[str, list[str]]

READ_OPEN = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
READ_LOCAL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
WRITE_OPEN = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DESTRUCTIVE_OPEN = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


def _cache_path(base_url: str) -> Path | None:
    env = os.environ.get("SIGNALEYES_SPEC_CACHE")
    if env is not None:
        return Path(env) if env else None
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    host_key = hashlib.sha1(base_url.encode()).hexdigest()[:12]
    return cache_dir / "signaleyes-mcp" / f"openapi-{host_key}.json"


def _read_cache(cache: Path) -> dict[str, Any] | None:
    try:
        spec: dict[str, Any] = json.loads(cache.read_text())
        return spec
    except (OSError, ValueError):
        return None


def load_spec(base_url: str, max_age: float = 86_400) -> dict[str, Any]:
    """openapi.json is ~470 KB with no ETag; cache it a day so startup skips the fetch.

    The cache file is keyed by base_url, written atomically (concurrent HTTP
    workers may start together), and a corrupt file counts as a miss. A
    stale cache also lets the server boot while the API is unreachable.
    SIGNALEYES_SPEC_CACHE overrides the path; empty disables caching.
    """
    cache = _cache_path(base_url)
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        cached = _read_cache(cache)
        if cached is not None:
            return cached
    try:
        resp = httpx2.get(f"{base_url}/openapi.json", timeout=30)
        resp.raise_for_status()
    except (httpx2.HTTPError, OSError):
        stale = _read_cache(cache) if cache else None
        if stale is not None:
            return stale
        raise
    spec: dict[str, Any] = resp.json()
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(resp.text)
        os.replace(tmp, cache)
    return spec


def is_excluded(path: str) -> bool:
    return any(re.match(p, path) for p in EXCLUDED_PATTERNS)


def build_source_registry(
    spec: dict[str, Any],
) -> tuple[SourceRegistry, SourceVariants, list[str]]:
    """Derive the /source/{entity} operation support matrix from the live spec.

    Almost every /source/* path is one of four shapes: "list" (the polygon
    query), "by_id" (.../{id}), "metadata" (.../metadata), or "batch"
    (POST .../batch, id lookup). Some entities need a `variant` path segment
    (e.g. /source/building/{entity}/...) and some don't, and which shapes
    exist at all varies per entity.

    Returns the registry (entity -> shape -> {variant_required: path
    template, using the placeholder "{variant}"/"{id}"}), the valid variant
    values per entity where the spec declares them as an enum, plus the
    literal (non-normalized) GET "list" paths — used to exclude the
    single-page generated tools that `fetch_all_pages` already covers, and
    their POST siblings that `post_large_polygon` replaces.
    """
    registry: SourceRegistry = {}
    variants: SourceVariants = {}
    list_paths: list[str] = []
    for path, path_item in spec.get("paths", {}).items():
        if not path.startswith("/source/") or is_excluded(path):
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
                and last.strip("{}") not in VARIANT_PARAM_NAMES
                and method == "get"
            ):
                shape = "by_id"
            elif method == "get":
                shape = "list"
                list_paths.append(path)
            else:
                continue

            norm = ["{variant}" if s.strip("{}") in VARIANT_PARAM_NAMES else s for s in segs]
            entity_segs = norm[2:] if shape == "list" else norm[2:-1]
            entity_key = "_".join(s for s in entity_segs if s != "{variant}").replace("-", "_")
            requires_variant = "{variant}" in norm
            registry.setdefault(entity_key, {}).setdefault(shape, {})[requires_variant] = "/".join(
                norm
            )
            for param in path_item[method].get("parameters", []):
                enum = param.get("schema", {}).get("enum")
                if param.get("name") in VARIANT_PARAM_NAMES and enum:
                    variants[entity_key] = list(enum)
    return registry, variants, list_paths


def route_pattern(template: str) -> str:
    """Turn a normalized registry template back into a RouteMap-matchable regex.

    RouteMap.pattern matches the spec's literal path template
    ("/source/building/{entity}/{id}"), so the normalized "{variant}"
    placeholder becomes an alternation over the real param names.
    """
    parts = [
        r"\{(?:entity|identifier)\}" if seg == "{variant}" else re.escape(seg)
        for seg in template.split("/")
    ]
    return "^" + "/".join(parts) + "$"


def build_route_maps(registry: SourceRegistry, list_paths: list[str]) -> list[RouteMap]:
    """Which spec operations become generated tools, and which the hand-written ones replace."""
    maps = [RouteMap(pattern=p, mcp_type=MCPType.EXCLUDE) for p in EXCLUDED_PATTERNS]
    maps.append(
        RouteMap(pattern=DUPLICATE_POST_CONCEPT_PATTERN, methods=["POST"], mcp_type=MCPType.EXCLUDE)
    )
    # Every GET list query collapses into fetch_all_pages. Its POST sibling
    # declares the body as a bare JSON string (the WKT polygon), which
    # FastMCP sends as raw bytes with no Content-Type, so every generated
    # one 400s upstream; post_large_polygon replaces them all.
    maps.extend(
        RouteMap(pattern=rf"^{re.escape(p)}$", methods=["GET", "POST"], mcp_type=MCPType.EXCLUDE)
        for p in list_paths
    )
    # by_id / metadata / batch collapse into get_object_by_id,
    # get_objects_metadata and post_objects_by_ids.
    maps.extend(
        RouteMap(pattern=route_pattern(p), mcp_type=MCPType.EXCLUDE)
        for shapes in registry.values()
        for shape, variants in shapes.items()
        if shape in ("by_id", "metadata", "batch")
        for p in variants.values()
    )
    # Task reads collapse into manage_task / wait_for_task; DELETE stays its
    # own tool so approving reads never approves deletes.
    maps.extend(
        RouteMap(pattern=p, methods=["GET"], mcp_type=MCPType.EXCLUDE)
        for p in (
            r"^/task/\{task_id\}$",
            r"^/task/\{task_id\}/describe$",
            r"^/task/\{task_id\}/metadata$",
            r"^/task/\{task_id\}/result$",
        )
    )
    maps.append(RouteMap(methods=["GET", "POST", "DELETE"], mcp_type=MCPType.TOOL))
    return maps


def task_input_tools(spec: dict[str, Any]) -> frozenset[str]:
    """MCP names of generated tools that take task_id_* inputs (the GET concepts)."""
    names: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            params = {p.get("name") for p in op.get("parameters", [])}
            if params & set(TASK_INPUT_ARGS):
                op_id = str(op.get("operationId", ""))
                names.add(MCP_NAMES.get(op_id, op_id))
    return frozenset(names)


def write_tool_names(spec: dict[str, Any]) -> frozenset[str]:
    """MCP names of generated tools that create or delete server-side state."""
    names: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for method, op in path_item.items():
            if method in ("post", "delete") and isinstance(op, dict):
                op_id = str(op.get("operationId", ""))
                names.add(MCP_NAMES.get(op_id, op_id))
    return frozenset(names)


def resolve_source_path(
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
            "(check list_source_entities for the valid values)."
        )
    return templates[has_variant].format(variant=variant, **path_kwargs)


def describe_source_registry(
    registry: SourceRegistry, enums: SourceVariants | None = None
) -> dict[str, dict[str, Any]]:
    described: dict[str, dict[str, Any]] = {}
    for entity, shapes in sorted(registry.items()):
        entry: dict[str, Any] = {}
        if enums and entity in enums:
            entry["variants"] = enums[entity]
        for shape in ("list", "by_id", "metadata", "batch"):
            forms = shapes.get(shape)
            if forms is None:
                continue
            if True in forms and False in forms:
                entry[shape] = "variant optional"
            elif True in forms:
                entry[shape] = "variant required"
            else:
                entry[shape] = "no variant"
        described[entity] = entry
    return described


def annotate_openapi_component(route: HTTPRoute, component: object) -> None:
    """Trim upstream boilerplate and mark generated tools read/write/destructive."""
    if not isinstance(component, OpenAPITool):
        return
    if component.description and PAGINATION_BOILERPLATE in component.description:
        component.description = component.description.split(PAGINATION_BOILERPLATE)[0].rstrip()
    method = route.method.upper()
    if method == "GET":
        component.annotations = READ_OPEN
    elif method == "DELETE":
        component.annotations = DESTRUCTIVE_OPEN
    elif method == "POST":
        component.annotations = WRITE_OPEN
