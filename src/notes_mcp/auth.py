"""Server-side OAuth 2.1 with GitHub as the upstream identity (Phase 5).

Why not delegate auth entirely (e.g. Cloudflare Access Managed OAuth)?
Access returns its 401 without `WWW-Authenticate`, and the claude.ai
web/mobile connector dies at Connect (anthropics/claude-ai-mcp#410,
closed not-planned). So this server IS the OAuth authorization server.
The MCP SDK supplies the endpoints
(metadata, DCR, /authorize, /token, PKCE S256 verification, and the 401 with
`WWW-Authenticate: Bearer resource_metadata=...`); this module supplies the
provider behind them.

Flow: Claude registers via DCR → /authorize redirects to GitHub → GitHub
redirects back to /auth/callback → we verify the GitHub login is the allowed
user → issue our authorization code → SDK's /token exchanges it (verifying
PKCE) for JWTs we mint.

Tokens are stateless HS256 JWTs signed with a secret persisted in the state
dir, so access survives container restarts. Client registrations persist to
the same dir; pending authorize states and issued codes are in-memory only
(they live for minutes).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger("notes_mcp.auth")

ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTH_CODE_TTL = 300
PENDING_STATE_TTL = 600
SCOPES = ["notes"]
# DCR is unauthenticated by spec; a registration grants nothing without the
# allowed GitHub login, so this cap only bounds state bloat from bots. If
# bots ever fill it and a legitimate reconnect is blocked, delete
# <state_dir>/clients.json and restart.
MAX_CLIENTS: int = 50  # tests shrink this; annotate so it isn't Literal[50]

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def _cid(client: OAuthClientInformationFull) -> str:
    """client_id is Optional on the SDK model but always set after DCR."""
    assert client.client_id is not None
    return client.client_id


class CallbackError(Exception):
    """Raised by handle_callback; message is safe to show in the browser."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _PendingAuth:
    client_id: str
    params: AuthorizationParams
    created_at: float = field(default_factory=time.monotonic)


class GitHubOAuthProvider:
    """OAuthAuthorizationServerProvider using GitHub to authenticate the one
    allowed user."""

    def __init__(
        self,
        *,
        issuer_url: str,
        github_client_id: str,
        github_client_secret: str,
        allowed_login: str,
        state_dir: Path,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.github_client_id = github_client_id
        self.github_client_secret = github_client_secret
        self.allowed_login = allowed_login
        self.state_dir = state_dir
        self.callback_url = f"{self.issuer_url}/auth/callback"

        self._pending: dict[str, _PendingAuth] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # The upstream identity exchange, as a swappable attribute so tests
        # can substitute a fake without monkey-patching a bound method.
        self.github_exchange: Callable[[str], Awaitable[str]] = self._github_login_for_code

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._secret = self._load_or_create_secret()
        self._clients_file = self.state_dir / "clients.json"
        self._load_clients()

    # ------------------------------------------------------------------
    # persistence

    def _load_or_create_secret(self) -> str:
        secret_file = self.state_dir / "signing_secret"
        if secret_file.is_file():
            secret = secret_file.read_text().strip()
            if secret:
                return secret
        secret = secrets.token_hex(32)
        secret_file.touch(mode=0o600)
        secret_file.write_text(secret)
        logger.info("generated new token signing secret", extra={"path": str(secret_file)})
        return secret

    def _load_clients(self) -> None:
        if not self._clients_file.is_file():
            return
        try:
            raw = json.loads(self._clients_file.read_text())
            self._clients = {
                cid: OAuthClientInformationFull.model_validate(info) for cid, info in raw.items()
            }
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("could not load persisted clients", extra={"reason": str(exc)})

    def _save_clients(self) -> None:
        data = {cid: c.model_dump(mode="json") for cid, c in self._clients.items()}
        self._clients_file.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------------
    # JWTs

    def _mint(self, token_type: str, client_id: str, subject: str, ttl: int) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer_url,
                "aud": "notes-mcp",
                "sub": subject,
                "client_id": client_id,
                "scopes": SCOPES,
                "type": token_type,
                "iat": now,
                "exp": now + ttl,
                "jti": secrets.token_hex(8),
            },
            self._secret,
            algorithm="HS256",
        )

    def _verify(self, token: str, token_type: str) -> dict[str, Any] | None:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience="notes-mcp",
                issuer=self.issuer_url,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            return None
        if claims.get("type") != token_type:
            return None
        return claims

    # ------------------------------------------------------------------
    # GitHub upstream (overridable in tests)

    async def _github_login_for_code(self, code: str) -> str:
        """Exchange the GitHub authorization code and return the user login."""
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.github_client_id,
                    "client_secret": self.github_client_secret,
                    "code": code,
                    "redirect_uri": self.callback_url,
                },
            )
            token_resp.raise_for_status()
            gh_token = token_resp.json().get("access_token")
            if not gh_token:
                raise CallbackError("GitHub did not return an access token", status=502)
            user_resp = await client.get(
                GITHUB_USER_URL,
                headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/json"},
            )
            user_resp.raise_for_status()
            login = user_resp.json().get("login")
            if not isinstance(login, str):
                raise CallbackError("GitHub did not return a user login", status=502)
            return login

    # ------------------------------------------------------------------
    # OAuthAuthorizationServerProvider protocol

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if _cid(client_info) not in self._clients and len(self._clients) >= MAX_CLIENTS:
            logger.warning(
                "client registration rejected: limit reached",
                extra={"max_clients": MAX_CLIENTS},
            )
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="registration limit reached",
            )
        self._clients[_cid(client_info)] = client_info
        self._save_clients()
        logger.info(
            "registered client",
            extra={
                "client_id": client_info.client_id,
                "redirect_uris": [str(u) for u in client_info.redirect_uris or []],
            },
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.monotonic()
        self._pending = {
            s: p for s, p in self._pending.items() if now - p.created_at < PENDING_STATE_TTL
        }
        state = secrets.token_urlsafe(32)
        self._pending[state] = _PendingAuth(client_id=_cid(client), params=params)
        return construct_redirect_uri(
            GITHUB_AUTHORIZE_URL,
            client_id=self.github_client_id,
            redirect_uri=self.callback_url,
            state=state,
            scope="read:user",
        )

    async def handle_callback(self, code: str | None, state: str | None) -> str:
        """GitHub redirected back to us; finish the flow.

        Returns the URL to redirect the MCP client (Claude) to.
        """
        if not code or not state:
            raise CallbackError("missing code or state")
        pending = self._pending.pop(state, None)
        if pending is None or time.monotonic() - pending.created_at > PENDING_STATE_TTL:
            raise CallbackError("unknown or expired state; restart the connect flow")

        login = await self.github_exchange(code)
        if login != self.allowed_login:
            logger.warning("rejected github login", extra={"login": login})
            raise CallbackError(f"GitHub user {login!r} is not allowed", status=403)

        our_code = secrets.token_urlsafe(32)
        self._codes[our_code] = AuthorizationCode(
            code=our_code,
            scopes=pending.params.scopes or SCOPES,
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=login,
        )
        logger.info("authorization granted", extra={"login": login, "client_id": pending.client_id})
        return construct_redirect_uri(
            str(pending.params.redirect_uri), code=our_code, state=pending.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self._codes[authorization_code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        subject = authorization_code.subject or self.allowed_login
        return OAuthToken(
            access_token=self._mint("access", _cid(client), subject, ACCESS_TOKEN_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=self._mint("refresh", _cid(client), subject, REFRESH_TOKEN_TTL),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = self._verify(refresh_token, "refresh")
        if claims is None or claims.get("client_id") != _cid(client):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=_cid(client),
            scopes=list(claims.get("scopes", SCOPES)),
            expires_at=int(claims["exp"]),
            subject=str(claims.get("sub", "")) or None,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        subject = refresh_token.subject or self.allowed_login
        return OAuthToken(
            access_token=self._mint("access", _cid(client), subject, ACCESS_TOKEN_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes or refresh_token.scopes),
            refresh_token=self._mint("refresh", _cid(client), subject, REFRESH_TOKEN_TTL),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._verify(token, "access")
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "")),
            scopes=list(claims.get("scopes", [])),
            expires_at=int(claims["exp"]),
            subject=str(claims.get("sub", "")) or None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless JWTs: nothing to delete; tokens die at exp. Single-user
        # threat model accepts this (rotate the signing secret to force it).
        return None
