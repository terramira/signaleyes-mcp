"""Orchestrator-facing FastMCP client for this server.

In-process: pass the FastMCP instance from `build_server`. Over the network:
pass the streamable-HTTP URL (typically `http://127.0.0.1:8000/mcp`).

`call_tool` never raises on a tool-level failure: it returns a `ToolOutcome`
the specialist agent can feed back to the model. The server tags every tool
error with a recovery hint and `[retryable=yes|no]` (see `client.tag_error`),
so this module parses that tag instead of keeping its own error table.
FastMCP injects W3C `traceparent` into MCP `_meta`, so a Langfuse/OTEL span
on the orchestrator links to this server's spans.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp import Client
from mcp.types import TextResourceContents

from signaleyes_mcp.client import parse_tagged_error

SKILL_URI = "skill://signaleyes/SKILL.md"


@dataclass(frozen=True)
class ToolOutcome:
    """One MCP tool call, ready to hand back to an orchestrator agent."""

    ok: bool
    data: Any = None
    error: str | None = None
    retryable: bool = False
    hint: str | None = None

    def __str__(self) -> str:
        if self.ok:
            return str(self.data)
        return self.error or "tool error"


def classify_tool_error(message: str) -> ToolOutcome:
    """Read the server's `[retryable=...]` tag; an untagged error is a client-side one."""
    retryable, hint = parse_tagged_error(message)
    if hint is None:
        lower = message.lower()
        if "timed out" in lower or "timeout" in lower:
            retryable, hint = True, "Transient timeout. Retry."
        else:
            hint = "Fix the arguments; do not retry the same call."
    return ToolOutcome(ok=False, error=message, retryable=retryable, hint=hint)


def _error_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    if content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str) and text:
            return text
    structured = getattr(result, "structured_content", None)
    if structured:
        return str(structured)
    return "tool error"


def _result_data(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return getattr(result, "data", None)


connect = Client  # `async with connect(server_or_url, timeout=...) as client:`


async def read_skill(client: Client[Any]) -> str:
    """Load the SignalEyes workflow skill (read this before the first tool call)."""
    contents = await client.read_resource(SKILL_URI)
    if not isinstance(contents, list):
        raise RuntimeError(f"unexpected result from {SKILL_URI}")
    texts = [c.text for c in contents if isinstance(c, TextResourceContents)]
    if not texts:
        raise RuntimeError(f"no text content at {SKILL_URI}")
    return "\n".join(texts)


async def list_tools(client: Client[Any], *, read_only: bool | None = None) -> list[Any]:
    """List tools. `read_only=True` keeps annotated reads; default is the full set."""
    tools = await client.list_tools()
    if read_only is None:
        return tools
    return [t for t in tools if bool(t.annotations and t.annotations.read_only_hint) is read_only]


async def bind(client: Client[Any], *, read_only: bool | None = None) -> tuple[str, list[Any]]:
    """Skill text + tool list. Call once when the specialist agent starts."""
    return await read_skill(client), await list_tools(client, read_only=read_only)


async def call_tool(
    client: Client[Any],
    name: str,
    arguments: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolOutcome:
    """Call a tool and return structured success/failure (never raises on tool errors)."""
    try:
        result = await client.call_tool(name, arguments, raise_on_error=False, **kwargs)
    except Exception as exc:
        return classify_tool_error(str(exc))
    if result.is_error:
        return classify_tool_error(_error_text(result))
    return ToolOutcome(ok=True, data=_result_data(result))


async def call_tools(
    client: Client[Any],
    calls: Sequence[tuple[str, dict[str, Any] | None]],
) -> list[ToolOutcome]:
    """Run independent tool calls concurrently (fan-out)."""
    return list(await asyncio.gather(*(call_tool(client, name, args) for name, args in calls)))
