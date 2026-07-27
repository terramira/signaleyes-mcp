from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from signaleyes_mcp import server as signaleyes_mcp


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("SIGNALEYES_USERNAME", "SIGNALEYES_PASSWORD", "SIGNALEYES_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_missing_username_exits(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main()
    assert exc.value.code == 1


def test_missing_password_exits(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main()
    assert exc.value.code == 1


def test_missing_both_exits(clean_env):
    with pytest.raises(SystemExit) as exc:
        signaleyes_mcp.main()
    assert exc.value.code == 1


def test_missing_credentials_prints_to_stderr(clean_env, capsys):
    with pytest.raises(SystemExit):
        signaleyes_mcp.main()
    assert "SIGNALEYES_USERNAME" in capsys.readouterr().err


def test_success_builds_and_runs_server_with_default_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main()

    fake_build.assert_called_once_with("https://api.neo.nl/v1", "user", "pw")
    fake_server.run.assert_called_once_with()


def test_success_uses_custom_base_url(clean_env, monkeypatch):
    monkeypatch.setenv("SIGNALEYES_USERNAME", "user")
    monkeypatch.setenv("SIGNALEYES_PASSWORD", "pw")
    monkeypatch.setenv("SIGNALEYES_BASE_URL", "https://api-dev.neo.nl/v1")

    fake_server = MagicMock()
    fake_build = MagicMock(return_value=fake_server)
    monkeypatch.setattr(signaleyes_mcp, "build_server", fake_build)

    signaleyes_mcp.main()

    fake_build.assert_called_once_with("https://api-dev.neo.nl/v1", "user", "pw")
    fake_server.run.assert_called_once_with()
