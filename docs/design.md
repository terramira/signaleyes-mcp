# Design and development notes

How the server is built and why, plus development, testing and release. For installing and using it, see the [README](../README.md).

MCP server for NEO's [SignalEyes API](https://api.neo.nl/v1/docs) (the API
exposed by [SuperAPI](https://github.com/terramira/SuperAPI)) — a geospatial
REST API combining building, tree, road, water, solar panel, and other
sources with chainable analytical concepts (density, closest, buffer,
overlay, shadow modelling, ...) and predefined analyses (3/30/300, BAF+).

At startup the server fetches the live `openapi.json` from the API. Most of
its ~115 operations are one of four repeated shapes per source entity
(polygon query, id lookup, batch id lookup, metadata) — so instead of turning
every operation into its own MCP tool (which is what `FastMCP.from_openapi`
does by default, and what this server did originally: ~90+ tools, most of
them near-duplicates that differ only by entity name), those four shapes
collapse into a handful of generic `entity`-parameterized tools. The
resulting tool set is under 30, and the entity/operation support matrix
behind the generic tools (which entities exist, whether each needs a
`variant` sub-type) is derived from `spec["paths"]` at startup rather than
hand-typed — so, like the rest of this server, it can't drift from what the
API currently exposes even as sources are added or reshaped.

Raw OGC WFS, vector tile, and raster tile endpoints are excluded (binary/XML
responses an LLM tool call can't use) — as are DSM/DTM source/pages,
shadow-modelling, and usage-records, whose `openapi.json` entries claim
`application/json` but which actually return an `image/tiff` raster or
`.xlsx` export. `/subscriptions` and `/token/*` are also excluded:
`/subscriptions` just duplicates `whoami`, and `/token/*` issues OAuth2
bearer tokens this server never uses (every request is already signed with
HTTP Basic auth). Everything else becomes a tool, generated or hand-written.

**Discovery:**

- **`list_source_entities`** — every `/source/{entity}` dataset and which of
  query / by-id / metadata / batch it supports, and whether each needs a
  `variant` sub-type. Call this first when unsure what's available; it's
  what the other tools' docstrings point back to instead of enumerating ~20
  entity names inline everywhere.

**Fetching data** (both replace the auto-generated per-entity tools,
including the broken ones — see the quirk note below):

- **`fetch_all_pages`** — `GET /source/{entity}` paginates via an
  `X-Total-Pages` response header, invisible to a plain auto-generated tool
  (it only sees the JSON body). This walks every page and merges them into
  one `FeatureCollection`, supporting every filter the API does
  (`filter_string`, `geo_type`, `simplify`, `sample`, `dissolve`,
  `intersect_centroid`, `attributes`/`groupby` for `entity="thirdparty"`).
  `max_pages` defaults to 10 (not "all") so an unexpectedly large area
  doesn't flood the caller's context — raise it explicitly once the area's
  size is known, or set `respond_async=True` to have the API gather every
  page itself and hand back a `task_id` instead.
- **`post_large_polygon`** — POSTs the polygon in the request body instead
  of the URL, for AOIs too large for a GET query. Also the fix for a real
  bug: the auto-generated `post_objects_*` tools for every
  `POST /source/{entity}` route always 400 upstream, because the API
  declares those request bodies as a bare JSON string (the WKT polygon, not
  `{"polygon": ...}`) and FastMCP's OpenAPI client sends bare-string bodies
  as raw unencoded bytes with no `Content-Type` instead of JSON-encoding
  them. This tool builds the request by hand so the body is serialized
  correctly, and surfaces the same `X-Total-Pages`/`X-Page-Number` headers
  `fetch_all_pages` does.

**Looking up specific records** (replace ~50 near-duplicate auto-generated
tools — one `GET .../{id}`, one `GET .../metadata`, and one
`POST .../batch` per entity):

- **`get_object_by_id`** — `entity` + `object_id` (+ `variant` where
  required) → one object.
- **`post_objects_by_ids`** — the same, batched: up to 100 ids in one call.
- **`get_objects_metadata`** — field/schema metadata for an entity, so
  `filter_string` column names don't have to be guessed.

**Task management:**

- **`wait_for_tasks`** — resolves many task ids in one call (describe by
  default, or result); waits run concurrently server-side, so a batch
  takes as long as its slowest task. Per-task failures are returned under
  that id instead of failing the batch.
- **`wait_for_task`** — many endpoints (concepts, analyses, async source
  fetches) return a `task_id` instead of a result. This polls
  `GET /task/{id}` until it's `done`/`failed` and returns the result in one
  call, instead of the caller manually re-polling turn after turn. Polling
  starts at 0.3s and backs off to `poll_interval`. A failed task is
  reported with its error and left for `delete_task` (the wait tools are
  read-only); a task still running at `max_wait` can simply be waited on
  again — the API refuses to delete queued/running tasks. The API answers
  a failed task's status with HTTP 500 and `status: failed` in the body;
  the poller and `manage_task(view="status")` read the body first.
- The GET `concept_*` tools wait for their `task_id_*` inputs themselves
  (a middleware polls them under one deadline), so a fetch → buffer →
  sjoin chain can be fired back to back without a `wait_for_task` per hop.
- **`manage_task`** — the four `GET /task/{id}(/*)` reads
  (`status`/`describe`/`metadata`/`result`) behind one `view` argument.
  Deletion is deliberately not a `view` here — it stays the auto-generated
  `delete_task` tool, so an MCP client that allowlists reads on `manage_task`
  doesn't thereby approve deleting the task too.

**Credentials:**

- **`whoami`** — checks credentials by fetching `/subscriptions`, so a
  broken setup fails with one clear tool call instead of dozens of opaque
  401s.

- **`usage_summary`** — the API reports usage per literal path, so every
  `/task/{id}/describe` call is its own row (760 rows / 160 KB for a month of
  agent use). This folds task and record ids to `{id}`, sums per route, and
  returns the 50 busiest rows.

Everything else (the `/concept/*` and `/analyses/*` tools, `post_geojson`,
`delete_task`) stays auto-generated — their parameters are
genuinely different per operation, so collapsing them would trade a large
tool count for a large, mostly-optional parameter list on a couple of tools,
which is worse for an agent to reason about, not better.

All hand-written tools (and every generated one) raise with the API's actual
`detail` message on 4xx/5xx responses, not just a bare status code — a 422
tells you *why* the polygon or filter was rejected. The server also sets
`instructions` (visible to MCP clients that surface it) with a short
end-to-end workflow: discover → fetch → look up → chain a concept → wait for
the task — since knowing *which* tool exists is only half the problem an
agent runs into here.

The auto-generated tools that stay auto-generated (`concept_*`, `analyses_*`,
`post_geojson`, `delete_task`) get short, stable names via
`mcp_names` instead of FastMCP's default — the raw OpenAPI `operationId`,
which for this API's FastAPI-generated spec repeats the function name and
path (`get_task_concept_overlay_concept_overlay_get`). Every session that
connects pays for tool-name length, so this isn't just cosmetic. Where an
operation has both a GET (task_id-chaining) and POST (live-entity) form —
`concept_closest`, `concept_density_area`, `concept_density_count` — only
the GET one is exposed: it's the one the skill recommends, and each POST
schema was another 2-3k chars on every session's tool listing. The
"fetch all pages via response headers" recipe upstream appends to every
description is stripped for the same reason.

**A quirk worth knowing, not a bug:** the auto-generated `concept_aggregation`
and `concept_difference_area` tools take `geojson_list` — a *list* of
FeatureCollections (`[{"type": "FeatureCollection", "features": [...]}]`),
not one bare FeatureCollection. (Older FastMCP versions flattened this
parameter under the misleading name `geojson`; with fastmcp >= 3.4.6 the
schema names it `geojson_list` directly.)

## Layout

- `spec.py` — loads/caches `openapi.json` (keyed by base URL, atomic
  write, stale copy used when the API is down), decides which operations
  become tools, derives the source-entity registry.
- `client.py` — the retrying httpx client, error text and hints, task
  status/polling.
- `server.py` — the hand-written tools, middlewares, and CLI.
- `agent.py` — the orchestrator-side client.

## External WFS / PDOK imports

The existing **signaleyes** MCP server now includes:

- `inspect_external_dataset(url, protocol="wfs")`: discover the WFS layers.
- `import_external_dataset(url, layer, polygon, protocol="wfs")`: download
  a bounded GeoJSON snapshot, clip it to a WGS84 WKT polygon, and upload it
  through `/source/geojson` using this server's existing authenticated client.

These tools use the existing SignalEyes authentication. Example workflow:

> Use the PDOK/OGC discovery MCP to find the BAG WFS link. In signaleyes,
> inspect_external_dataset, then import_external_dataset for the pand layer
> within POLYGON((4.8928 52.3720,4.8935 52.3720,4.8935 52.37245,4.8928 52.37245,4.8928 52.3720)).
> Poll manage_task with view="status" until done, then get view="describe".

The secure downloader is included in this package. WFS 2.0 must advertise
GeoJSON output and CRS84 or EPSG:4326. Request bounds follow the advertised
CRS axis order; returned GeoJSON must use longitude-first CRS84 coordinates.
GML-only services are unsupported. Limits: 5,000 features, 20 pages, 10 MiB
and 60 seconds of download time; private addresses and redirects are blocked.
No credentials are sent to PDOK. Empty selections create no task; incomplete
downloads are not uploaded. An upload timeout may still have created a task:
check before retrying.

Existing `fetch_all_pages` / `post_large_polygon`, `manage_task`, `wait_for_task`
and `concept_*` tools fetch data, manage tasks and run spatial analysis.
For counts and summaries, prefer `manage_task(view="describe")`; `wait_for_task`
downloads the task result into the conversation. This imports a snapshot,
not a live link that automatically refreshes.

## Export a task as GeoJSON

`export_task_file(task_id)` downloads a completed task through the existing
authenticated SignalEyes client and saves its original GeoJSON to
`SIGNALEYES_OUTPUT_DIR` (default `~/signaleyes_outputs`). The MCP response
contains only the path, filename, byte size and feature count, so a result
larger than the chat tool limit never enters the model's conversation.
Exports are bounded to 50 MB and 120 seconds; failed exports remove partial
files. Existing tasks are unchanged and no centroid task is needed.

Export a completed task with
`export_task_file(task_id="YOUR_COMPLETED_TASK_ID")`. The tool returns the
saved file path without rerunning the analysis or returning inline GeoJSON.

Open the resulting `.geojson` file in your preferred GIS application.
The file stays on your computer; the tool returns a local path, not a
public download link or an uploaded attachment. Credentials remain local.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # fill in SIGNALEYES_USERNAME / SIGNALEYES_PASSWORD
```

Credentials are SignalEyes API credentials (the same username/password used
with the `/token` endpoints or HTTP Basic auth) — ask NEO for API access if
you don't have one.

`.env` is loaded automatically (via `python-dotenv`, from the current
directory or a parent) when running `serve`/`smoke` — no need to `export` it
into your shell. A real environment variable (e.g. one set in an MCP
client's own config) always takes precedence over `.env`.

`uv run signaleyes-mcp` takes a subcommand (defaults to `serve` if omitted —
this is what an MCP client actually launches):

```bash
uv run signaleyes-mcp            # serve over stdio (default)
uv run signaleyes-mcp serve --transport http --port 8000
uv run signaleyes-mcp smoke      # one real query against SignalEyes, to sanity-check credentials/setup
uv run signaleyes-mcp smoke --entity tree --polygon "POLYGON ((...))"
```

`smoke` needs `SIGNALEYES_USERNAME`/`PASSWORD` set (it hits the live API).
The test suite is `uv run pytest` (everything but `test_live_spec.py` is mocked).

## Orchestrator / multi-agent use

This server is meant to be one specialist in an agent orchestrator, not a
chatbot with 90+ near-duplicate tools.

**stdio** — one client, one process (Claude Code, Copilot, a single worker).

**Streamable HTTP** — several agents sharing one server:

```bash
uv run signaleyes-mcp serve --transport http --host 127.0.0.1 --port 8000
```

Then point workers at `http://127.0.0.1:8000/mcp`. HTTP runs **stateless**
(no session affinity), so concurrent workers can share one process. In Python:

```python
from signaleyes_mcp import bind, call_tool, call_tools, connect

async with connect("http://127.0.0.1:8000/mcp") as client:
    skill, tools = await bind(client)  # load this before the first tool call
    entities = await call_tool(client, "list_source_entities", {})
    if not entities.ok:
        # entities.retryable / entities.hint tell the agent whether to retry
        ...
    # independent reads can fan out:
    pages = await call_tools(
        client,
        [
            ("get_objects_metadata", {"entity": "tree"}),
            ("get_objects_metadata", {"entity": "building"}),
        ],
    )
```

`call_tool` returns a `ToolOutcome` (`ok`, `data`, `error`, `retryable`,
`hint`) instead of raising on a 4xx/5xx from the API — the specialist agent
can put that back in context and recover. FastMCP already injects W3C
`traceparent` into MCP `_meta`, so a Langfuse/OpenTelemetry span on the
orchestrator links to this server's spans. Configure an OTEL exporter in the
orchestrator process (no extra dependency here), for example Langfuse's
OTLP endpoint.

`bind` is the current pattern for teaching a specialist agent the workflow
without stuffing it into every tool schema: discover → fetch or describe →
wait for tasks → chain GET concepts. The same text is also exposed as MCP
resource `skill://signaleyes/SKILL.md`. Every tool error — generated tools
included — ends with a recovery hint and `[retryable=yes|no]`, decided once
in `client.recovery_hint` and attached by a server middleware; `agent.py`
parses that tag instead of keeping its own table.

A FeatureCollection over `SIGNALEYES_MAX_TOOL_RESPONSE_BYTES` (default
50_000 — above that Claude Code moves the result to a file the model
never sees inline) is cut to a 3-feature preview and marked
`truncated: true` / `total_features` (a partial list of geometries is not
usable data; `export_task_file` is the way to get all of it);
`fetch_all_pages` also stops paging once past the cap (and marks an early
stop `truncated` without claiming a total). For counts/stats use
`respond_async=True` then `manage_task(view="describe")` instead of pulling
geometries. `describe` output drops the top-value lists of
(near-)unique columns such as ids. An async fetch the API will later fail
for exceeding its 500-page limit is rejected up front.

Retries live in `client.RetryingClient.send` (not a custom transport, so
httpx keeps `HTTP(S)_PROXY`/`NO_PROXY` support): 429 always, connect
errors and 502/503/504 only for requests that cannot create a task —
`GET /concept/*`, `GET /analyses/*`, anything with `Prefer: respond-async`,
and every POST are never re-sent. 500 is not retried: that is how the API
reports a failed task.

Read-only tools advertise `readOnlyHint`; `delete_task` advertises
`destructiveHint`, so an orchestrator can allowlist the former.

## Configuration

| Env var | Required | Default |
|---|---|---|
| `SIGNALEYES_USERNAME` | yes | — |
| `SIGNALEYES_PASSWORD` | yes | — |
| `SIGNALEYES_BASE_URL` | no | `https://api.neo.nl/v1` |
| `SIGNALEYES_MAX_TOOL_RESPONSE_BYTES` | no | `50000` (set `0` to disable truncation) |
| `SIGNALEYES_SPEC_CACHE` | no | `$XDG_CACHE_HOME/signaleyes-mcp/openapi-<base-url-hash>.json`, reused for 24h and as a fallback when the API is unreachable (set empty to disable) |

Set `SIGNALEYES_BASE_URL=https://api-dev.neo.nl/v1` to point at the dev
environment instead.

## Testing

```bash
uv run pytest --cov=signaleyes_mcp --cov-report=term-missing
```

Unit tests mock the OpenAPI fetch and HTTP client — no network or
credentials needed, ~99% statement coverage:

- `test_build_server.py` — route-map exclusion/inclusion (including the new
  entity-registry-driven exclusions), auth wiring, spec fetch, error
  propagation.
- `test_custom_tools.py` — `wait_for_task`, `fetch_all_pages` (merging,
  variants, page cap, filter/attributes/groupby passthrough, respond_async),
  `post_large_polygon`, `whoami`, `get_object_by_id`, `post_objects_by_ids`,
  `get_objects_metadata`, `list_source_entities`, `manage_task` — including
  the registry's error paths (unknown entity, missing/unsupported variant).
- `test_client.py` — the retrying client's policy per request kind, error
  tagging in both error formats, failed-task status behind a 500.
- `test_cli.py` — the `serve`/`smoke` argument parser and `run_smoke`.
- `test_main.py` — `main()`'s subcommand dispatch, env-var validation, and
  success path.

`test_live_spec.py` is marked `network` and excluded from the default run
(`addopts = -m "not network"` in `pyproject.toml`); it hits the real public
spec and checks the tool set lands in a sane, *bounded* shape (20-45 tools —
low enough to catch a regression back toward one tool per HTTP operation,
high enough to allow for upstream additions), that every consolidated tool
is present, and that no excluded endpoints leak through. Run it explicitly
with `uv run pytest -m network`.

Type-check with `uv run mypy src/`.

## CI/CD

- **`ci.yml`** — on every push/PR, in parallel: `ruff format --check` +
  `ruff check`; `mypy src/`; the test suite with coverage across Python
  3.11/3.12/3.13 (HTML coverage report uploaded as a build artifact); and a
  `pip-audit` dependency vulnerability scan. All jobs use uv's dependency
  cache and cancel superseded runs on the same branch.
- **`network-canary.yml`** — runs the live-spec test daily (`workflow_dispatch`
  also available) so an upstream API change surfaces without coupling every
  PR to `api.neo.nl`'s uptime.
- **`release.yml`** — on pushing a tag `vX.Y.Z`: verifies the tag matches
  `pyproject.toml`'s version, re-runs lint/tests, then creates a GitHub
  release.
- **Dependabot** — weekly updates for both the `uv` and `github-actions`
  ecosystems.
- **`.pre-commit-config.yaml`** — `ruff check --fix` + `ruff format` before
  commit; install with `uv run pre-commit install`.

To cut a release:

```bash
uv version --bump patch   # or minor / major — updates pyproject.toml + uv.lock
git commit -am "Bump version to $(uv version --short)"
git tag "v$(uv version --short)"
git push && git push --tags
```

Not included: automatic version bumps from commit messages
(release-please/semantic-release) — bumping stays a deliberate
`uv version --bump` step; and Codecov — it needs linking this private repo
to an external account, so coverage stays inline in CI logs plus the HTML
artifact instead.
