---
name: signaleyes
description: Query NEO SignalEyes geospatial data and chain analyses via MCP tools. Use when fetching buildings, trees, roads, water, solar panels, or other source entities, or when running density/closest/buffer/overlay/sjoin concepts over a polygon.
---

# SignalEyes MCP

Geospatial REST API (buildings, trees, roads, water, solar panels, …) plus
chainable concepts (density, closest, buffer, overlay, sjoin, …).

Prefer `manage_task(view="describe")` over pulling full geometries whenever
you only need counts or attribute stats.

## Flow

1. `list_source_entities` — which `entity`/`variant` exist. Do not guess names.
2. Unfamiliar entity? `get_objects_metadata` before writing `filter_string`.
3. Fetch: `fetch_all_pages` (GET). Polygon too large for a URL → `post_large_polygon`.
   - `max_pages` defaults to 10. Raise it only after you know the AOI is small.
   - Large/slow → `respond_async=True`, then step 5.
4. Look up records: `get_object_by_id` / `post_objects_by_ids` (max 100 ids,
   all must lie in the caller's AOI).
5. Any result with a `task_id` (or `id` + status `queued`/`running`): `wait_for_task`.
   Need a summary, not geometries? `manage_task(task_id, view="describe")`.
6. Concepts:
   - GET `concept_*` — combine two completed fetches via `task_id_primary_entity`
     / `task_id_secondary_entity`. Prefer these.
   - POST `concept_*_from_entity` — run against a live entity + polygon.
7. 401/403 → `whoami` once, then stop retrying other tools until credentials work.

## Do not

- Do not poll `manage_task(view="status")` in a loop; use `wait_for_task`.
- Do not call `delete_task` unless the user asked to drop a task.
- Do not dump multi-page FeatureCollections into context to "count them".
- `concept_aggregation` / `concept_difference_area`: the `geojson` argument
  must be `{"geojson_list": [...]}`, not a bare FeatureCollection.

## Errors

Tool errors include the API `detail` and a recovery hint.
- 401/403 → `whoami` once, then stop other tools until credentials work.
- 422 → fix the arguments; do not retry the same call.
- 429 → wait, then retry.
- POST 502/503/504 → do not re-POST (a task may already exist); `manage_task`.
Transient GET 429/5xx is retried by the server — wait for that rather than
re-issuing immediately.
