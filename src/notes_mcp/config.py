"""Environment-driven configuration.

Secrets accept two forms, 12-factor style: `NAME` holds the value directly,
or `NAME_FILE` points at a file containing it (the direct form wins if both
are set). Plain env vars suit single-admin deploys; the _FILE form suits
Docker-secret setups.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised at startup when required configuration is missing or invalid."""


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _secret_env(name: str) -> str | None:
    """Resolve a secret from $NAME or $NAME_FILE (direct value wins)."""
    direct = os.environ.get(name)
    if direct and direct.strip():
        return direct.strip()
    file_raw = os.environ.get(f"{name}_FILE")
    if not file_raw:
        return None
    path = Path(file_raw)
    if not path.is_file():
        raise ConfigError(f"{name}_FILE {path} is missing")
    value = path.read_text().strip()
    if not value:
        raise ConfigError(f"{name}_FILE {path} is empty")
    return value


@dataclass(frozen=True)
class Settings:
    notes_repo_path: Path
    notes_repo_url: str | None
    notes_repo_branch: str
    github_token: str | None
    git_author_name: str
    git_author_email: str
    host: str
    port: int
    log_level: str
    script_timeout_default: int
    script_timeout_max: int
    public_url: str | None
    github_oauth_client_id: str | None
    github_oauth_client_secret: str | None
    github_allowed_login: str | None
    oauth_state_dir: Path

    @property
    def auth_enabled(self) -> bool:
        return self.public_url is not None

    @classmethod
    def from_env(cls) -> Settings:
        repo_path = Path(os.environ.get("NOTES_REPO_PATH", "/repo"))
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        log_level = os.environ.get("LOG_LEVEL", "info").lower()

        port = _int_env("MCP_PORT", 8000)
        script_timeout_default = _int_env("SCRIPT_TIMEOUT_DEFAULT", 60)
        script_timeout_max = _int_env("SCRIPT_TIMEOUT_MAX", 300)

        if log_level not in {"debug", "info", "warning", "error"}:
            raise ConfigError(
                f"LOG_LEVEL must be one of debug/info/warning/error, got {log_level!r}"
            )

        public_url = (os.environ.get("PUBLIC_URL") or "").rstrip("/") or None
        gh_client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID") or None
        gh_oauth_secret = _secret_env("GITHUB_OAUTH_CLIENT_SECRET")
        gh_allowed_login = os.environ.get("GITHUB_ALLOWED_LOGIN") or None
        if public_url and not (gh_client_id and gh_oauth_secret and gh_allowed_login):
            raise ConfigError(
                "PUBLIC_URL is set (auth enabled) but GITHUB_OAUTH_CLIENT_ID / "
                "GITHUB_OAUTH_CLIENT_SECRET(_FILE) / GITHUB_ALLOWED_LOGIN are missing"
            )
        if (gh_client_id or gh_oauth_secret) and not public_url:
            raise ConfigError("GITHUB_OAUTH_* set but PUBLIC_URL is missing")

        return cls(
            notes_repo_path=repo_path,
            notes_repo_url=os.environ.get("NOTES_REPO_URL") or None,
            notes_repo_branch=os.environ.get("NOTES_REPO_BRANCH", "main"),
            github_token=_secret_env("GITHUB_TOKEN"),
            git_author_name=os.environ.get("GIT_AUTHOR_NAME", "Claude MCP"),
            git_author_email=os.environ.get(
                "GIT_AUTHOR_EMAIL", "claude-mcp@users.noreply.github.com"
            ),
            host=host,
            port=port,
            log_level=log_level,
            script_timeout_default=script_timeout_default,
            script_timeout_max=script_timeout_max,
            public_url=public_url,
            github_oauth_client_id=gh_client_id,
            github_oauth_client_secret=gh_oauth_secret,
            github_allowed_login=gh_allowed_login,
            oauth_state_dir=Path(os.environ.get("OAUTH_STATE_DIR", "/data")),
        )

    def validate_startup(self) -> None:
        """Refuse to start on unusable configuration."""
        has_tree = self.notes_repo_path.is_dir()
        if not has_tree and not self.notes_repo_url:
            raise ConfigError(
                f"NOTES_REPO_PATH {self.notes_repo_path} does not exist and "
                "NOTES_REPO_URL is not set — nothing to serve and nothing to clone."
            )
        if (
            self.notes_repo_url
            and not (self.notes_repo_path / ".git").exists()
            and has_tree
            and any(self.notes_repo_path.iterdir())
        ):
            raise ConfigError(
                f"NOTES_REPO_PATH {self.notes_repo_path} is non-empty but not a "
                "git repo; refusing to clone over it."
            )
