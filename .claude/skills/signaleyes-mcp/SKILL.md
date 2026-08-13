---
name: signaleyes-mcp
description: Use whenever the signaleyes-mcp MCP server's tools are available and the task involves geospatial data from NEO's SignalEyes API — fetching buildings, trees, roads, water, solar panels, parcels, or other source entities for an area of interest; running a spatial concept (buffer, overlay, spatial join, density, closest, clip) on that data; or answering questions like "what's within N meters of X", "where is there free space", "how many X are near Y". Covers the async task_id pattern every non-trivial call goes through, how to discover which entities/variants exist, and reusable patterns for composing fetch → buffer → overlay/sjoin chains — none of which is visible from a single tool's schema alone.
---

# signaleyes-mcp

`signaleyes-mcp` exposes NEO's SignalEyes API as ~28 MCP tools (down from
~90 auto-generated ones — most of the API's operations collapse into a
handful of generic `entity`-parameterized tools; see that repo's README for
why). The tool schemas cover *what each tool takes*. This skill covers what
they don't: the async task pattern every real call goes through, and how to
compose multiple calls to answer a compound question — the part that
otherwise gets re-derived by trial and error every session.

## Discover before guessing

Call **`list_source_entities`** first if you don't already know which
entity/variant to use. It reflects whatever the live API currently exposes
— don't assume a name from memory or from an old example in this file.
Use **`get_objects_metadata(entity)`** to see an entity's real attribute
names before writing a `filter_string` guess.

## The async task pattern (read this before calling anything)

Almost nothing returns data directly. The normal shape is:

1. Fetch source data: **`fetch_all_pages`** (GET, small-to-medium AOI) or
   **`post_large_polygon`** (POST, AOI too big for a URL). Both return a
   `FeatureCollection` directly by default, or — with `respond_async=True`
   — a `task_id` immediately, for large/slow queries.
2. Run a concept on it (buffer, overlay, sjoin, ...): these take
   `task_id_primary_entity` (and often `task_id_secondary_entity`), not raw
   geometry. **You need a `task_id`, which means you need step 1's result
   stored as a task — not just its features.** In practice this means:
   using `fetch_all_pages`'s `respond_async=True`, or `post_large_polygon`
   (which always goes through a task), or posting already-fetched features
   directly into a task via the `post_geojson_*` tool.
3. Concept tools themselves return a **new** `task_id` — they don't return
   data either.
4. **`wait_for_task(task_id)`** resolves any `task_id` to its actual result
   (polls until done, raises on failure). Use **`manage_task(task_id,
   view="describe")`** first instead if you just want a feature/attribute
   summary without pulling the full result into context.

Chain rule: pass `task_id`, never raw geometry, between steps. Fetch
independent sources in parallel — don't serialize calls that don't depend
on each other.

## Composing multi-step answers

For "within N meters", "free space", "nearest X", "how many X per Y" and
similar compound questions, see **[references/patterns.md](references/patterns.md)**
— reusable fetch → buffer → overlay/sjoin chains, not derivable from any
single tool's schema. **[references/examples.md](references/examples.md)**
has worked end-to-end examples using real tool names and parameters.

## Concept tools: which one, and how

Concept tools are intentionally still individual auto-generated MCP tools
(named like `get_task_concept_overlay_concept_overlay_get`,
`run_concept_aggregation_concept_aggregation_post`) rather than collapsed —
their parameters are genuinely different per operation. Two shapes exist:

- **GET, task-chaining** (`buffer`, `clip`, `closest`, `density_area`,
  `density_count`, `overlay`, `sjoin`, `sjoin_nearest`): take
  `task_id_primary_entity` (+ `task_id_secondary_entity` for the two-input
  ones). This is the one to reach for by default — it's what
  `references/patterns.md` uses throughout, and it composes uniformly.
- **POST, live-entity** (`closest`, `density_area`, `density_count` again,
  plus `aggregation`/`difference_area`/`shadow_modelling` which have no GET
  form at all): post GeoJSON features directly instead of chaining a
  task_id for the primary side. Only reach for these over the GET form if
  you have a concrete reason to (e.g. an operation with no GET
  equivalent). **Known quirk**: some of these tools' body parameter is
  named `geojson` in its schema but actually expects
  `{"geojson_list": [...]}`, not a bare FeatureCollection — check the
  signaleyes-mcp README's "quirk" note if a call 422s unexpectedly.

Exact auto-generated tool names can shift if the upstream API's
`operationId`s change — if a name below 404s or isn't found, search the
current tool list for the operation instead of assuming the name is stale
elsewhere too.

## Common filter_string values

A curated (not live-verified) snapshot of categorical values worth knowing
up front — see **[references/filters.md](references/filters.md)**. Prefer
`get_objects_metadata` when it disagrees with this file.
