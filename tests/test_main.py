from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from signaleyes_mcp import server as signaleyes_mcp


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("SIGNALEYES_USERNAME", "SIGNALEYES_PASSWORD", "SIGNALEYES_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # Prevent a real .env file on disk (e.g. a dev's local credentials) from
    # leaking into these tests via main()'s load_dotenv() call.
    monkeypatch.setattr(signaleyes_mcp, "load_dotenv", lambda *a, **kw: None)


# main() takes an explicit argv so tests never fall through to pytest's own
# real sys.argv (main(argv=None) would parse *that*, not "no arguments").


def test_missing_username_exits(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main([])
    assert exc.value.code == 1


def test_missing_password_exits(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main([])
    assert exc.value.code == 1


def test_missing_both_exits(clean_env):
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main([])
    assert exc.value.code == 1


def test_missing_credentials_prints_to_stderr(clean_env, capsys):
    with pytest.raises(SystemExit):
        signaleyes_mcp.main([])
    assert "SIGNALEYES_USERNAME" in capsys.readouterr().err


def test_success_builds_and_runs_server_with_default_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main([])

    fake_build.assert_called_once_with("https://api.neo.nl/v1", "user", "pw")
    fake_server.run.assert_called_once_with()


def test_explicit_serve_subcommand_behaves_like_no_args(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main(["serve"])

    fake_build.assert_called_once_with("https://api.neo.nl/v1", "user", "pw")
    fake_server.run.assert_called_once_with()


def test_success_uses_custom_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")
    monkeypatch.setenv("SIGNALEYES_BASE_URL", "https://api-dev.neo.nl/v1")

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main([])

    fake_build.assert_called_once_with("https://api-dev.neo.nl/v1", "user", "pw")
    fake_server.run.assert_called_once_with()


# --- .env loading -------------------------------------------------------


def test_serve_calls_load_dotenv_before_checking_credentials(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")

    fake_load_dotenv = MagicMock()
    monkeypatch.setattr(signaleyes_mcp, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(signaleyes_mcp, "build_server", MagicMock(return_value=MagicMock()))

    signaleyes_mcp.main([])

    fake_load_dotenv.assert_called_once_with()


def test_env_file_fills_in_credentials_missing_from_the_environment(clean_env, monkeypatch):
    # Regression test: a .env file with credentials must actually reach
    # os.environ. Simulates load_dotenv() finding one, the way a real .env
    # file in the project directory would.
    def fake_load_dotenv(*args, **kwargs):
        monkeypatch.setenv("SIGNALEYES_USERNAME", "from-dotenv")
        monkeypatch.setenv("SIGNALEYES_PASSWORD", "from-dotenv-pw")

    monkeypatch.setattr(signaleyes_mcp, "load_dotenv", fake_load_dotenv)

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main([])

    fake_build.assert_called_once_with("https://api.neo.nl/v1", "from-dotenv", "from-dotenv-pw")


def test_real_environment_variable_takes_precedence_over_dotenv(clean_env, monkeypatch):
    # load_dotenv()'s default (override=False) means an explicitly-set env
    # var (e.g. from an MCP client's own config) must win over the .env file.
    monkeypatch.setenv("SIGNALEYES_USERNAME", "explicit-user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "explicit-pw")

    def fake_load_dotenv(*args, **kwargs):
        os.environ.setdefault("SIGNALEYES_USERNAME", "from-dotenv")
        os.environ.setdefault("SIGNALEYES_PASSWORD", "from-dotenv-pw")

    monkeypatch.setattr(signaleyes_mcp, "load_dotenv", fake_load_dotenv)

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main([])

    fake_build.assert_called_once_with("https://api.neo.nl/v1", "explicit-user", "explicit-pw")


# --- `test` subcommand -------------------------------------------------------


def test_test_subcommand_does_not_require_credentials(clean_env, monkeypatch):
    fake_run_tests = MagicMock(return_value=0)
    monkeypatch.setattr(signaleyes_mcp, "run_tests", fake_run_tests)

    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main(["test"])

    fake_run_tests.assert_called_once_with()
    assert exc.value.code == 0


def test_test_subcommand_propagates_pytest_exit_code(clean_env, monkeypatch):
    monkeypatch.setattr(signaleyes_mcp, "run_tests", MagicMock(return_value=3))

    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main(["test"])

    assert exc.value.code == 3


# --- `smoke` subcommand -------------------------------------------------------


def test_smoke_subcommand_requires_credentials(clean_env):
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main(["smoke"])
    assert exc.value.code == 1


def test_smoke_subcommand_calls_run_smoke_with_parsed_args(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")

    fake_run_smoke = MagicMock()

    async def fake_coro(*args, **kwargs):
        fake_run_smoke(*args, **kwargs)
        return 0

    monkeypatch.setattr(signaleyes_mcp, "run_smoke", fake_coro)

    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main(["smoke", "--entity", "tree", "--polygon", "POLYGON ((0 0))"])

    assert exc.value.code == 0
    fake_run_smoke.assert_called_once_with(
        "https://api.neo.nl/v1", "user", "pw", "tree", "POLYGON ((0 0))"
    )
