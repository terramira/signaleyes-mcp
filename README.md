# signaleyes-mcp

MCP server for NEO's [SignalEyes API](https://api.neo.nl/v1/docs) (the API
exposed by [SuperAPI](https://github.com/terramira/SuperAPI)) — a geospatial
REST API combining building, tree, road, water, solar panel, and other
sources with chainable analytical concepts (density, closest, buffer,
overlay, shadow modelling, ...) and predefined analyses (3/30/300, BAF+).

Tools aren't hand-written. At startup the server fetches the live
`openapi.json` from the API and turns every HTTP operation into an MCP tool
via `FastMCP.from_openapi` — so it always mirrors whatever endpoints the API
currently exposes, without needing updates when the API changes. Raw OGC WFS,
vector tile, and raster tile endpoints are excluded (binary/XML responses an
LLM tool call can't use); everything else becomes a tool.

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

Unit tests (`test_build_server.py`, `test_main.py`) mock the OpenAPI fetch
and HTTP client, covering route-map exclusion/inclusion, auth wiring, and
`main()`'s env-var validation — no network or credentials needed, 100%
statement coverage. `test_live_spec.py` (marked `network`) hits the real
public spec and checks the generated tool set lands in a sane shape (100+
tools, no dupes, no excluded endpoints leaking through) — a canary for
upstream API/spec changes.

## CI/CD

- **`ci.yml`** — on every push/PR: `ruff format --check` + `ruff check`,
  then the full test suite with coverage.
- **`release.yml`** — on pushing a tag `vX.Y.Z`: verifies the tag matches
  `pyproject.toml`'s version, re-runs lint/tests, then creates a GitHub
  release.

To cut a release:

```bash
uv version --bump patch   # or minor / major — updates pyproject.toml + uv.lock
git commit -am "Bump version to $(uv version --short)"
git tag "v$(uv version --short)"
git push && git push --tags
```
