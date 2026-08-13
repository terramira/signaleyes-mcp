# Capability findings

Written proposals from the `mcp-capability-check` workflow
(`.github/workflows/mcp-capability-check.yml`), which runs weekly and
checks whether a recent FastMCP or MCP-spec release introduced a primitive
that could replace hand-rolled code in `src/signaleyes_mcp/server.py`.

Each file here is a **finding, not an implementation** — what changed
upstream, what it could replace, and a sketch of the change. Turning one
into an actual code change is a separate, deliberate PR (researched, then
verified against the live API, the same way the rest of this server's
hand-written tools were).

Once a finding has been implemented (or deliberately rejected), delete its
file — this directory is a queue, not a changelog.
