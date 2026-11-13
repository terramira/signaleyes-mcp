from __future__ import annotations

import asyncio

import httpx2
import pytest

from signaleyes_mcp.server import SMOKE_ENTITIES, _build_arg_parser, run_smoke


def _response(status_code=200, json=None, headers=None) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        json=json,
        headers=headers or {},
        request=httpx2.Request("GET", "https://example.test/"),
    )


# --- argument parser ----------------------------------------------------


def test_no_args_means_no_command():
    args = _build_arg_parser().parse_args([])
    assert args.command is None


def test_serve_http_transport_args():
    args = _build_arg_parser().parse_args(
        ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "9000"]
    )
    assert args.command == "serve"
    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_smoke_defaults_entity_to_none_and_polygon_to_sample():
    args = _build_arg_parser().parse_args(["smoke"])
    assert args.command == "smoke"
    assert args.entity is None
    assert "POLYGON" in args.polygon


def test_smoke_accepts_entity_and_polygon_overrides():
    args = _build_arg_parser().parse_args(
        ["smoke", "--entity", "tree", "--polygon", "POLYGON ((0 0))"]
    )
    assert args.entity == "tree"
    assert args.polygon == "POLYGON ((0 0))"


def test_smoke_rejects_unknown_entity():
    with pytest.raises(SystemExit):
        _build_arg_parser().parse_args(["smoke", "--entity", "not-a-real-entity"])


# --- run_smoke ------------------------------------------------------------


def test_run_smoke_checks_credentials_then_queries_entity(async_client_get_queue, capsys):
    async_client_get_queue.responses.extend(
        [
            _response(json={"plan": "pro"}),
            _response(json={"features": [{"id": 1}]}, headers={"X-Total-Pages": "2"}),
        ]
    )

    result = asyncio.run(
        run_smoke(
            "https://example.test/v1", "user", "pass", entity="building", polygon="POLYGON ((0 0))"
        )
    )

    assert result == 0
    assert async_client_get_queue.calls[0]["url"] == "/subscriptions"
    assert async_client_get_queue.calls[1]["url"] == "/source/building"
    assert async_client_get_queue.calls[1]["params"] == {"polygon": "POLYGON ((0 0))", "page": 1}

    out = capsys.readouterr().out
    assert "Credentials OK" in out
    assert "1 feature(s) on page 1 of 2" in out


def test_run_smoke_picks_a_random_entity_when_none_given(async_client_get_queue, monkeypatch):
    monkeypatch.setattr("signaleyes_mcp.server.random.choice", lambda choices: "tree")
    async_client_get_queue.responses.extend(
        [
            _response(json={"plan": "pro"}),
            _response(json={"features": []}, headers={"X-Total-Pages": "1"}),
        ]
    )

    asyncio.run(run_smoke("https://example.test/v1", "user", "pass"))

    assert async_client_get_queue.calls[1]["url"] == "/source/tree"


def test_run_smoke_entity_choices_are_all_valid_source_names():
    # Sanity: the choices offered on the CLI are exactly what run_smoke can pick from.
    assert set(SMOKE_ENTITIES) == {"building", "tree", "road", "water", "solarpanel"}
