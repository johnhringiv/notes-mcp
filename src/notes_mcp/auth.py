"""Server-side OAuth 2.1 for the Claude connector, plus admin web sessions.

Why not delegate auth entirely (e.g. Cloudflare Access Managed OAuth)?
Access returns its 401 without `WWW-Authenticate`, and the claude.ai
web/mobile connector dies at Connect (anthropics/claude-ai-mcp#410,
closed not-planned). So this server IS the OAuth authorization server.
The MCP SDK supplies the endpoints
(metadata, DCR, /authorize, /token, PKCE S256 verification, and the 401 with
`WWW-Authenticate: Bearer resource_metadata=...`); this module supplies the
provider behind them.

Flow: Claude registers via DCR → /authorize sends the browser to our login
page (/auth/login) → the person either signs in with GitHub (→ /auth/callback)
or enters an access code an admin gave them (→ /auth/code) → members.py maps
that to a principal or refuses → we issue our authorization code → the SDK's
/token exchanges it (verifying PKCE) for JWTs we mint.

Tokens are stateless HS256 JWTs signed with a secret persisted in the state
dir, so access survives container restarts. A token's `sub` names the
principal and `gen` the member generation; every use re-resolves them, so
revoking a member, replacing their code or dropping an admin from
ADMIN_USERS takes effect on the next request. Client registrations persist;
pending logins and issued codes are in-memory only (they live for minutes).

The admin page reuses the GitHub leg: /admin/login starts a pending login
of purpose "admin", and the callback answers it with a session cookie (its
own JWT type, 8 h) instead of an authorization code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from notes_mcp.members import GitHubUser, MemberStore

logger = logging.getLogger("notes_mcp.auth")

ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
ADMIN_SESSION_TTL = 8 * 3600
AUTH_CODE_TTL = 300
PENDING_STATE_TTL = 600
MAX_CODE_ATTEMPTS = 5  # per pending login; codes are unguessable anyway
SCOPES = ["notes"]
# DCR is unauthenticated by spec; a registration grants nothing without an
# allowed login, so this cap only bounds state bloat from bots. If bots ever
# fill it and a legitimate reconnect is blocked, delete
# <state_dir>/clients.json and restart.
MAX_CLIENTS: int = 50  # tests shrink this; annotate so it isn't Literal[50]

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


def _cid(client: OAuthClientInformationFull) -> str:
    """client_id is Optional on the SDK model but always set after DCR."""
    assert client.client_id is not None
    return client.client_id


class CallbackError(Exception):
    """Raised during login; message is safe to show in the browser."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class GitHubUpstream:
    """GitHub OAuth app: authenticates people and looks up accounts by name."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        callback_url: str,
        transport: httpx.AsyncBaseTransport | None = None,  # tests inject a mock
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15, transport=self._transport)

    def authorize_url(self, state: str) -> str:
        return construct_redirect_uri(
            GITHUB_AUTHORIZE_URL,
            client_id=self.client_id,
            redirect_uri=self.callback_url,
            state=state,
            scope="read:user",
        )

    @staticmethod
    def _user(data: dict[str, Any]) -> GitHubUser:
        user_id, login = data.get("id"), data.get("login")
        if not isinstance(user_id, int) or not isinstance(login, str):
            raise CallbackError("GitHub did not return a user id and login", status=502)
        return GitHubUser(id=user_id, login=login)

    async def identify(self, code: str) -> GitHubUser:
        """Exchange the authorization code; return who logged in."""
        async with self._client() as client:
            token_resp = await client.post(
                GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.callback_url,
                },
            )
            token_resp.raise_for_status()
            gh_token = token_resp.json().get("access_token")
            if not gh_token:
                raise CallbackError("GitHub did not return an access token", status=502)
            user_resp = await client.get(
                f"{GITHUB_API}/user",
                headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/json"},
            )
            user_resp.raise_for_status()
            return self._user(user_resp.json())

    async def lookup(self, login: str) -> GitHubUser | None:
        """Resolve a username to its account (the admin page pins the id)."""
        async with self._client() as client:
            resp = await client.get(
                f"{GITHUB_API}/users/{login}", headers={"Accept": "application/json"}
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._user(resp.json())


@dataclass
class _PendingAuth:
    purpose: Literal["mcp", "admin"]
    client_id: str | None = None
    params: AuthorizationParams | None = None
    code_attempts: int = 0
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class CallbackResult:
    """Where the browser goes next, plus an admin session to set, if any."""

    redirect_url: str
    admin_session: str | None = None


def callback_url(issuer_url: str) -> str:
    return f"{issuer_url.rstrip('/')}/auth/callback"


class NotesOAuthProvider:
    """OAuthAuthorizationServerProvider backed by members.py."""

    def __init__(
        self,
        *,
        issuer_url: str,
        upstream: GitHubUpstream,
        members: MemberStore,
        state_dir: Path,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self.upstream = upstream
        self.members = members
        self.state_dir = state_dir

        self._pending: dict[str, _PendingAuth] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # The upstream identity exchange, as a swappable attribute so tests
        # can substitute a fake without monkey-patching a bound method.
        self.identify: Callable[[str], Awaitable[GitHubUser]] = upstream.identify
        self.lookup: Callable[[str], Awaitable[GitHubUser | None]] = upstream.lookup

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

    def _mint(self, token_type: str, client_id: str, subject: str, ttl: int, gen: int = 0) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": self.issuer_url,
                "aud": "notes-mcp",
                "sub": subject,
                "gen": gen,
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
        """Signature, expiry and type, then the principal must still resolve."""
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience="notes-mcp",
                issuer=self.issuer_url,
                options={"require": ["exp", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError:
            return None
        if claims.get("type") != token_type:
            return None
        if self.members.resolve(str(claims["sub"]), int(claims.get("gen", 0))) is None:
            return None
        return claims

    # ------------------------------------------------------------------
    # login (our pages call these)

    def _prune_pending(self) -> None:
        now = time.monotonic()
        self._pending = {
            s: p for s, p in self._pending.items() if now - p.created_at < PENDING_STATE_TTL
        }

    def _pending_for(self, state: str | None, purpose: str | None = None) -> _PendingAuth:
        self._prune_pending()
        pending = self._pending.get(state or "")
        if pending is None or (purpose is not None and pending.purpose != purpose):
            raise CallbackError("This sign-in link has expired or was already used.")
        return pending

    def login_page_ok(self, state: str | None) -> bool:
        try:
            self._pending_for(state, "mcp")
        except CallbackError:
            return False
        return True

    def github_url_for(self, state: str | None) -> str:
        """The GitHub leg of an MCP login started at /authorize."""
        self._pending_for(state, "mcp")
        assert state is not None
        return self.upstream.authorize_url(state)

    def begin_admin_login(self) -> str:
        self._prune_pending()
        state = secrets.token_urlsafe(32)
        self._pending[state] = _PendingAuth(purpose="admin")
        return self.upstream.authorize_url(state)

    def _finish_mcp(self, pending: _PendingAuth, subject: str) -> str:
        assert pending.params is not None and pending.client_id is not None
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
            subject=subject,
        )
        self.members.record_login(subject)
        logger.info(
            "authorization granted", extra={"subject": subject, "client_id": pending.client_id}
        )
        return construct_redirect_uri(
            str(pending.params.redirect_uri), code=our_code, state=pending.params.state
        )

    async def handle_callback(self, code: str | None, state: str | None) -> CallbackResult:
        """GitHub redirected back to us; finish an MCP or admin login."""
        if not code or not state:
            raise CallbackError("missing code or state")
        pending = self._pending_for(state)
        del self._pending[state]

        user = await self.identify(code)
        subject = self.members.login_github(user)
        if pending.purpose == "admin":
            if subject is None or not self.members.is_admin_subject(subject):
                logger.warning("rejected admin login", extra={"login": user.login})
                raise CallbackError(f"GitHub user {user.login!r} is not an admin", status=403)
            session = self._mint("admin_session", "admin", subject, ADMIN_SESSION_TTL)
            return CallbackResult(f"{self.issuer_url}/admin", admin_session=session)
        if subject is None:
            logger.warning("rejected github login", extra={"login": user.login, "id": user.id})
            raise CallbackError(f"GitHub user {user.login!r} is not allowed", status=403)
        return CallbackResult(self._finish_mcp(pending, subject))

    def handle_code_login(self, state: str | None, code: str) -> str:
        """An access code was submitted on the login page."""
        pending = self._pending_for(state, "mcp")
        subject = self.members.login_code(code)
        if subject is None:
            pending.code_attempts += 1
            if pending.code_attempts >= MAX_CODE_ATTEMPTS:
                self._pending.pop(state or "", None)
            logger.warning("rejected access code")
            raise CallbackError("that access code isn't valid", status=403)
        self._pending.pop(state or "", None)
        return self._finish_mcp(pending, subject)

    # ------------------------------------------------------------------
    # admin sessions

    def verify_admin_session(self, token: str | None) -> str | None:
        """The admin subject behind a session cookie, if still an admin."""
        if not token:
            return None
        claims = self._verify(token, "admin_session")
        if claims is None or not self.members.is_admin_subject(str(claims["sub"])):
            return None
        return str(claims["sub"])

    def csrf_token(self, session: str) -> str:
        return hmac.new(
            self._secret.encode(), b"csrf:" + session.encode(), hashlib.sha256
        ).hexdigest()

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
        self._prune_pending()
        state = secrets.token_urlsafe(32)
        self._pending[state] = _PendingAuth(purpose="mcp", client_id=_cid(client), params=params)
        return construct_redirect_uri(f"{self.issuer_url}/auth/login", state=state)

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

    def _issue(
        self, client: OAuthClientInformationFull, subject: str, scopes: list[str]
    ) -> OAuthToken:
        gen = self.members.generation(subject)
        return OAuthToken(
            access_token=self._mint("access", _cid(client), subject, ACCESS_TOKEN_TTL, gen),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
            refresh_token=self._mint("refresh", _cid(client), subject, REFRESH_TOKEN_TTL, gen),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        subject = authorization_code.subject
        if subject is None:  # handle_callback always sets it
            raise TokenError(error="invalid_grant", error_description="code has no subject")
        return self._issue(client, subject, authorization_code.scopes)

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
            subject=str(claims["sub"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # load_refresh_token just re-resolved the principal; nothing to add.
        if refresh_token.subject is None:
            raise TokenError(error="invalid_grant", error_description="token has no subject")
        return self._issue(client, refresh_token.subject, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._verify(token, "access")
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", "")),
            scopes=list(claims.get("scopes", [])),
            expires_at=int(claims["exp"]),
            subject=str(claims["sub"]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless JWTs: nothing to delete. Revocation is per principal
        # (member generation / ADMIN_USERS), checked on every use; rotate the
        # signing secret to revoke everyone.
        return None
