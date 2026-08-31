"""Orchestrator-facing FastMCP client for this server.

In-process: pass the FastMCP instance from `build_server`. Over the network:
pass the streamable-HTTP URL (typically `http://127.0.0.1:8000/mcp`).

`call_tool` never raises on a tool-level failure: it returns a `ToolOutcome`
the specialist agent can feed back to the model (retryable + recovery hint).
FastMCP already injects W3C `traceparent` into MCP `_meta` so a Langfuse/OTEL
span on the orchestrator links to this server's spans.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.telemetry import inject_trace_context
from mcp.types import TextResourceContents

SKILL_URI = "skill://signaleyes/SKILL.md"
_STATUS_RE = re.compile(r"'([45]\d{2})\s")


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
        msg = self.error or "tool error"
        if self.hint:
            return f"{msg} — {self.hint}"
        return msg


def classify_tool_error(message: str) -> ToolOutcome:
    """Map a tool error string to retryable + a single recovery hint."""
    status: int | None = None
    match = _STATUS_RE.search(message)
    if match:
        status = int(match.group(1))
    retryable, hint = _recovery(status, message)
    return ToolOutcome(ok=False, error=message, retryable=retryable, hint=hint)


def _recovery(status: int | None, message: str) -> tuple[bool, str]:
    if status in (401, 403):
        return False, "Call whoami once; do not retry other tools until credentials work."
    if status == 422 or "Invalid params" in message:
        return False, "Fix the arguments; do not retry the same call."
    if status == 429:
        return True, "Rate limited. Wait, then retry."
    if status == 409:
        return True, "Task is not done yet. Use wait_for_task."
    if status in (502, 503, 504):
        if "POST" in message:
            return False, "Do not retry this POST (a task may already exist). Check manage_task."
        return True, "Transient upstream error. Retry."
    lower = message.lower()
    if "timed out" in lower or "timeout" in lower:
        return True, "Transient timeout. Retry."
    return False, "Fix the arguments; do not retry the same call."


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


@asynccontextmanager
async def connect(target: FastMCP | str, *, timeout: float = 180.0) -> AsyncIterator[Client[Any]]:
    """Open an MCP session. `target` is a FastMCP instance or an HTTP/stdio target."""
    async with Client(target, timeout=timeout) as client:
        yield client


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
    return [t for t in tools if bool(t.annotations and t.annotations.readOnlyHint) is read_only]


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
    meta = inject_trace_context(kwargs.pop("meta", None))
    if meta is not None:
        kwargs["meta"] = meta
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
