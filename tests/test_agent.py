from __future__ import annotations

import asyncio

from signaleyes_mcp import (
    ToolOutcome,
    bind,
    build_server,
    call_tool,
    call_tools,
    classify_tool_error,
    connect,
    list_tools,
    read_skill,
)


def test_read_skill_and_list_entities(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> tuple[str, ToolOutcome]:
        async with connect(server) as client:
            skill = await read_skill(client)
            result = await call_tool(client, "list_source_entities", {})
            return skill, result

    skill, result = asyncio.run(go())
    assert "list_source_entities" in skill
    assert "respond_async=True" in skill
    assert result.ok
    assert result.data["agriparcel"]["list"] == "no variant"


def test_bind_loads_skill_and_tools(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> tuple[str, list[object]]:
        async with connect(server) as client:
            return await bind(client)

    skill, tools = asyncio.run(go())
    names = {t.name for t in tools}  # type: ignore[union-attr]
    assert "list_source_entities" in skill
    assert "list_source_entities" in names
    assert "delete_task" in names


def test_list_tools_can_keep_only_reads(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> list[str]:
        async with connect(server) as client:
            tools = await list_tools(client, read_only=True)
            return [t.name for t in tools]

    names = asyncio.run(go())
    assert "list_source_entities" in names
    assert "delete_task" not in names
    assert "post_large_polygon" not in names


def test_call_tool_returns_classified_error_on_unknown_entity(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> ToolOutcome:
        async with connect(server) as client:
            return await call_tool(client, "get_object_by_id", {"entity": "nope", "object_id": "1"})

    result = asyncio.run(go())
    assert not result.ok
    assert result.retryable is False
    assert result.error
    assert "nope" in result.error
    assert result.hint and "Fix the arguments" in result.hint


def test_call_tools_fans_out(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> list[ToolOutcome]:
        async with connect(server) as client:
            return await call_tools(
                client,
                [
                    ("list_source_entities", {}),
                    ("list_source_entities", {}),
                ],
            )

    results = asyncio.run(go())
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert results[0].data["agriparcel"]["list"] == "no variant"


def test_classify_reads_the_server_tag():
    out = classify_tool_error(
        "Client error '401 Unauthorized' for url 'https://x' — Call whoami once; do not "
        "retry other tools until credentials work. [retryable=no]"
    )
    assert not out.ok
    assert out.retryable is False
    assert out.hint and "whoami" in out.hint
    assert str(out).endswith("[retryable=no]")

    out = classify_tool_error(
        "HTTP error 429: x - {} — Rate limited. Wait, then retry. [retryable=yes]"
    )
    assert out.retryable is True
    assert out.hint == "Rate limited. Wait, then retry."


def test_classify_untagged_errors_are_client_side():
    assert classify_tool_error("Invalid params: entity").retryable is False
    timeout = classify_tool_error("Timed out while waiting for response to ClientRequest")
    assert timeout.retryable is True
    assert str(ToolOutcome(ok=True, data={"n": 1})) == "{'n': 1}"
    assert "tool error" in str(ToolOutcome(ok=False))


def test_error_text_and_result_data_fallbacks():
    from types import SimpleNamespace

    from signaleyes_mcp.agent import _error_text, _result_data

    assert _error_text(SimpleNamespace(content=[], structured_content={"d": 1})) == "{'d': 1}"
    assert _error_text(SimpleNamespace(content=[], structured_content=None)) == "tool error"
    assert _error_text(SimpleNamespace(content=[SimpleNamespace()], structured_content=None)) == (
        "tool error"
    )
    assert _result_data(SimpleNamespace(structured_content=None, data=7)) == 7


def test_call_tool_classifies_raised_client_errors(mock_spec_get, spy_async_client):
    server = build_server("https://example.test/v1", "user", "pass")

    async def go() -> ToolOutcome:
        async with connect(server) as client:

            async def boom(*_a: object, **_k: object) -> object:
                raise TimeoutError("call timed out")

            client.call_tool = boom  # type: ignore[method-assign]
            return await call_tool(client, "list_source_entities", {})

    result = asyncio.run(go())
    assert not result.ok
    assert result.retryable is True
