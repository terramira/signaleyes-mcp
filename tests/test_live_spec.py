"""Self-check: the public OpenAPI spec still yields a sane tool set.

No credentials needed — building the tool list only reads the spec.
Calling a tool would need SIGNALEYES_USERNAME/PASSWORD, not covered here.
"""

import asyncio

import pytest

from signaleyes_mcp import build_server

BASE_URL = "https://api.neo.nl/v1"

pytestmark = pytest.mark.network


def test_build_server_generates_tools():
    server = build_server(BASE_URL, username="unused", password="unused")
    tools = asyncio.run(server.list_tools())

    # Bounded, not just a floor: the whole point of the entity-registry
    # consolidation is a small tool set (~28 as of this writing — concept/
    # analyses/task/geojson auto-generated tools plus ~10 hand-written
    # ones). The upper bound is what actually catches a regression back
    # toward one tool per HTTP operation (~90+).
    assert 20 <= len(tools) <= 45, f"expected a consolidated ~20-45 tools, got {len(tools)}"

    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names generated"
    assert not any("wfs" in n for n in names), "WFS endpoints should be excluded"
    assert not any("tiles" in n for n in names), "tile endpoints should be excluded"
    for consolidated in (
        "get_object_by_id",
        "post_objects_by_ids",
        "get_objects_metadata",
        "list_source_entities",
        "manage_task",
        "fetch_all_pages",
        "post_large_polygon",
    ):
        assert consolidated in names, f"expected consolidated tool {consolidated!r}"
