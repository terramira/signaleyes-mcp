"""Self-check: the public OpenAPI spec still yields a sane tool set.

No credentials needed — building the tool list only reads the spec.
Calling a tool would need SIGNALEYES_USERNAME/PASSWORD, not covered here.
"""

import asyncio

from signaleyes_mcp import build_server

BASE_URL = "https://api.neo.nl/v1"


def test_build_server_generates_tools():
    server = build_server(BASE_URL, username="unused", password="unused")
    tools = asyncio.run(server.list_tools())

    assert len(tools) > 100, f"expected 100+ tools, got {len(tools)}"

    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names generated"
    assert not any("wfs" in n for n in names), "WFS endpoints should be excluded"
    assert not any("tiles" in n for n in names), "tile endpoints should be excluded"
