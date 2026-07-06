"""Tests for secret resolution and startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from notes_mcp.config import ConfigError, Settings, _secret_env
from notes_mcp.git_ops import GitOps


def test_secret_env_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "  tok-123  ")
    assert _secret_env("GITHUB_TOKEN") == "tok-123"


def test_secret_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "s"
    secret.write_text("tok-from-file\n")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(secret))
    assert _secret_env("GITHUB_TOKEN") == "tok-from-file"


def test_secret_env_direct_wins_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "s"
    secret.write_text("file-token")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(secret))
    assert _secret_env("GITHUB_TOKEN") == "env-token"


def test_secret_env_missing_or_bad_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    assert _secret_env("GITHUB_TOKEN") is None
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="missing"):
        _secret_env("GITHUB_TOKEN")
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(empty))
    with pytest.raises(ConfigError, match="empty"):
        _secret_env("GITHUB_TOKEN")


def test_auth_env_must_be_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PUBLIC_URL",
        "GITHUB_ALLOWED_LOGIN",
        "GITHUB_OAUTH_CLIENT_ID",
        "GITHUB_OAUTH_CLIENT_SECRET",
        "GITHUB_OAUTH_CLIENT_SECRET_FILE",
        "GITHUB_TOKEN",
        "GITHUB_TOKEN_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.example.com")
    with pytest.raises(ConfigError, match="GITHUB_OAUTH_CLIENT_ID"):
        Settings.from_env()
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "shh")
    with pytest.raises(ConfigError, match="GITHUB_ALLOWED_LOGIN"):
        Settings.from_env()
    monkeypatch.setenv("GITHUB_ALLOWED_LOGIN", "alice")
    settings = Settings.from_env()
    assert settings.auth_enabled
    assert settings.github_oauth_client_secret == "shh"


def test_git_env_carries_askpass_and_password(tmp_path: Path) -> None:
    ops = GitOps(repo_path=tmp_path, token="tok-xyz")
    env = ops._git_env()
    assert env["GIT_PASSWORD"] == "tok-xyz"
    askpass = Path(env["GIT_ASKPASS"])
    assert askpass.is_file()
    text = askpass.read_text()
    assert "printenv GIT_PASSWORD" in text
    assert "tok-xyz" not in text  # secret never written to disk


def test_git_env_without_token(tmp_path: Path) -> None:
    env = GitOps(repo_path=tmp_path)._git_env()
    assert "GIT_PASSWORD" not in env
    assert "GIT_ASKPASS" not in env
