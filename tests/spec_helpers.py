"""Tiny OpenAPI-spec builders shared by test modules.

Kept out of conftest.py deliberately: conftest is a pytest plugin module,
loaded by pytest's collection machinery rather than plain import — helpers
that other test modules import directly (not just via fixture injection)
belong in an ordinary importable module instead, so they keep working under
a direct module run or an alternative test runner.
"""

from __future__ import annotations


def make_spec(paths: dict) -> dict:
    return {"openapi": "3.0.0", "info": {"title": "test", "version": "1"}, "paths": paths}


_VARIANT_ENUMS = {"entity": ["detail", "surrounding"]}


def op(
    operation_id: str,
    path_params: tuple[str, ...] = (),
    description: str | None = None,
    query_params: tuple[str, ...] = (),
) -> dict:
    return {
        "operationId": operation_id,
        **({"description": description} if description else {}),
        "parameters": [
            {
                "name": p,
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    **({"enum": _VARIANT_ENUMS[p]} if p in _VARIANT_ENUMS else {}),
                },
            }
            for p in path_params
        ]
        + [{"name": q, "in": "query", "schema": {"type": "string"}} for q in query_params],
        "responses": {"200": {"description": "ok"}},
    }
