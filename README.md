<p align="center">
  <img src="docs/assets/terramira.svg" width="72" alt="Terramira">
</p>

<h1 align="center">signaleyes-mcp</h1>

<p align="center">
  Geospatial data and analysis from the
  <a href="https://api.neo.nl/v1/docs">SignalEyes API</a>, as tools for your coding agent.
</p>

---

An [MCP](https://modelcontextprotocol.io) server that lets Claude Code,
Cursor, GitHub Copilot and Codex query SignalEyes directly: buildings, trees,
roads, water, solar panels and more for any area, plus spatial analysis
(buffer, overlay, joins, density, nearest) chained on top.

Ask your agent things like:

- *"How many buildings within 50 m of the Oudegracht have solar panels?"*
- *"Which streets in this neighbourhood have the least tree cover?"*
- *"Import this PDOK layer and overlay it with our building data."*

It runs on your own machine with your own SignalEyes login. Nothing is
hosted: the agent starts it when needed, and your tasks and usage stay on
your account.

## Install

Once per machine, about five minutes.

**1. Prerequisites**

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Access to this private repo from `git` (`gh auth login` is the quickest way)
- Your own SignalEyes API username and password. Don't share a login: people
  on the same account can read and delete each other's tasks.

**2. Save your credentials**

Create `~/.signaleyes-mcp.env` (Windows: `%USERPROFILE%\.signaleyes-mcp.env`):

```bash
SIGNALEYES_USERNAME=your_username
SIGNALEYES_PASSWORD=your_password
```

On macOS/Linux, run `chmod 600 ~/.signaleyes-mcp.env` so only you can read
it. Every agent uses this one file.

**3. Check it works**

```bash
uvx --from git+https://github.com/terramira/signaleyes-mcp signaleyes-mcp smoke
```

You should see `Credentials OK` and a feature count.

**4. Add it to your agent**, then restart the agent

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --scope user signaleyes-mcp -- \
  uvx --from git+https://github.com/terramira/signaleyes-mcp signaleyes-mcp
```
</details>

<details>
<summary><b>Codex</b></summary>

```bash
codex mcp add signaleyes-mcp -- \
  uvx --from git+https://github.com/terramira/signaleyes-mcp signaleyes-mcp
```
</details>

<details>
<summary><b>Cursor</b> and <b>GitHub Copilot CLI</b></summary>

Cursor: `~/.cursor/mcp.json`. Copilot CLI: `~/.copilot/mcp-config.json`.

```json
{
  "mcpServers": {
    "signaleyes-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/terramira/signaleyes-mcp", "signaleyes-mcp"]
    }
  }
}
```
</details>

<details>
<summary><b>GitHub Copilot in VS Code</b></summary>

Run **MCP: Open User Configuration** and add the entry above, under a
top-level `"servers"` key instead of `"mcpServers"`.
</details>

**5. Try it**

> Use signaleyes to count the buildings around Utrecht Centraal.

The server teaches each agent how to use the API, so there is nothing else
to install.

## Tools

| | |
|---|---|
| **Discover** | `list_source_entities`, `get_objects_metadata`, `whoami` |
| **Fetch** | `fetch_all_pages`, `post_large_polygon`, `get_object_by_id`, `post_objects_by_ids` |
| **Analyse** | `concept_buffer`, `concept_overlay`, `concept_sjoin`, `concept_closest`, `concept_density_count` and other `concept_*` tools; `analyses_3_30_300`, `analyses_baf` |
| **Tasks** | `wait_for_task`, `wait_for_tasks`, `manage_task`, `delete_task` |
| **External data** | `inspect_external_dataset`, `import_external_dataset` (WFS / PDOK) |
| **Export** | `export_task_file` saves a result as GeoJSON for a GIS application |
| **Account** | `usage_summary` |

Large queries run as background tasks. Results too big for the agent's
context come back as a short preview; use `export_task_file` for the full
data.

## Updating

```bash
uvx --refresh --from git+https://github.com/terramira/signaleyes-mcp signaleyes-mcp smoke
```

Then restart your agent. To stay on a fixed version, add a tag to the URL:
`git+https://github.com/terramira/signaleyes-mcp@v0.1.0`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SIGNALEYES_USERNAME and SIGNALEYES_PASSWORD must be set` | `~/.signaleyes-mcp.env` is missing or misnamed. Redo step 2. |
| 401 or 403 on every call | Wrong credentials, or the area is outside your subscription. Ask the agent to run `whoami`. |
| `uvx` fails with a git or authentication error | Your machine can't clone the private repo. Run `gh auth login`. |
| Agent shows no SignalEyes tools | Restart the agent. If that doesn't help, use the full path from `which uvx` as the command. |
| Slow first start | The first run builds the package and downloads the API spec. Later starts are fast. |

## Configuration

Set these in `~/.signaleyes-mcp.env` or the agent's own `env` block.

| Variable | Default |
|---|---|
| `SIGNALEYES_USERNAME`, `SIGNALEYES_PASSWORD` | required |
| `SIGNALEYES_BASE_URL` | `https://api.neo.nl/v1` (dev: `https://api-dev.neo.nl/v1`) |
| `SIGNALEYES_MAX_TOOL_RESPONSE_BYTES` | `50000`; `0` disables the size cap |

## Development

```bash
git clone https://github.com/terramira/signaleyes-mcp && cd signaleyes-mcp
uv sync
uv run pytest
```

Design, the shared HTTP mode for orchestrators, testing and releases are in
[docs/design.md](docs/design.md).
