# signaleyes-mcp

MCP server for NEO's [SignalEyes API](https://api.neo.nl/v1/docs) (the API
exposed by [SuperAPI](https://github.com/terramira/SuperAPI)) — a geospatial
REST API combining building, tree, road, water, solar panel, and other
sources with chainable analytical concepts (density, closest, buffer,
overlay, shadow modelling, ...) and predefined analyses (3/30/300, BAF+).

Most tools aren't hand-written. At startup the server fetches the live
`openapi.json` from the API and turns every HTTP operation into an MCP tool
via `FastMCP.from_openapi` — so it always mirrors whatever endpoints the API
currently exposes, without needing updates when the API changes. Raw OGC WFS,
vector tile, and raster tile endpoints are excluded (binary/XML responses an
LLM tool call can't use); everything else becomes a tool.

On top of that, three tools are hand-written because the OpenAPI spec can't
express them:

- **`wait_for_task`** — many endpoints (concepts, analyses, async source
  fetches) return a `task_id` instead of a result. This polls
  `GET /task/{id}` until it's `done`/`failed` and returns the result in one
  call, instead of the caller manually re-polling turn after turn.
- **`fetch_all_pages`** — `GET /source/{entity}` paginates via an
  `X-Total-Pages` response header, invisible to the auto-generated tools
  (they only see the JSON body). This walks every page and merges them into
  one `FeatureCollection`.
- **`whoami`** — checks credentials by fetching `/subscriptions`, so a
  broken setup fails with one clear tool call instead of 100+ opaque 401s.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # fill in SIGNALEYES_USERNAME / SIGNALEYES_PASSWORD
```

Credentials are SignalEyes API credentials (the same username/password used
with the `/token` endpoints or HTTP Basic auth) — ask NEO for API access if
you don't have one.

Run directly to sanity-check it starts:

```bash
uv run signaleyes-mcp
```

## Register with an MCP client

**Claude Code:**

```bash
claude mcp add signaleyes-mcp \
  --env SIGNALEYES_USERNAME=your_username \
  --env SIGNALEYES_PASSWORD=your_password \
  -- uv run --directory /path/to/signaleyes-mcp signaleyes-mcp
```

**GitHub Copilot CLI** — add to `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "signaleyes-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/signaleyes-mcp", "signaleyes-mcp"],
      "env": {
        "SIGNALEYES_USERNAME": "your_username",
        "SIGNALEYES_PASSWORD": "your_password"
      }
    }
  }
}
```

## Configuration

| Env var | Required | Default |
|---|---|---|
| `SIGNALEYES_USERNAME` | yes | — |
| `SIGNALEYES_PASSWORD` | yes | — |
| `SIGNALEYES_BASE_URL` | no | `https://api.neo.nl/v1` |

Set `SIGNALEYES_BASE_URL=https://api-dev.neo.nl/v1` to point at the dev
environment instead.

## Testing

```bash
uv run pytest --cov=signaleyes_mcp --cov-report=term-missing
```

Unit tests mock the OpenAPI fetch and HTTP client — no network or
credentials needed, 100% statement coverage:

- `test_build_server.py` — route-map exclusion/inclusion, auth wiring, spec
  fetch, error propagation.
- `test_custom_tools.py` — `wait_for_task` (immediate/polled/failed/timeout),
  `fetch_all_pages` (merging, variants, page cap), `whoami`.
- `test_main.py` — `main()`'s env-var validation and success path.

`test_live_spec.py` is marked `network` and excluded from the default run
(`addopts = -m "not network"` in `pyproject.toml`); it hits the real public
spec and checks the generated tool set lands in a sane shape (100+ tools, no
dupes, no excluded endpoints leaking through). Run it explicitly with
`uv run pytest -m network`.

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
