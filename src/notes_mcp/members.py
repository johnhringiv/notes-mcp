"""Who may use the server, how they log in, and what they may touch.

Two kinds of principal:

- **Admins** are GitHub users named in ADMIN_USERS (server config). They
  have full access, may run scripts, and manage members on /admin. Each
  admin's numeric GitHub id is pinned on first login: usernames can be
  renamed and then re-registered by someone else, ids can't.
- **Members** are created on the admin page. A member logs in with GitHub
  (matched by pinned id, never by username) and/or an access code, and has
  per-path grants, each read-only ("ro") or read-write ("rw").

Grants live here, not in tokens: a token names its principal and the
member's generation, and every use resolves the current record. Editing
grants takes effect on the member's next tool call; bumping the generation
(new code, removed code, revoke) kills every session at once.

State persists as JSON in the OAuth state dir. Access codes are stored only
as SHA-256 hashes; they are 192-bit random, so a fast hash is enough.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from notes_mcp.access import FULL, Grants

Mode = Literal["ro", "rw"]
MODES: tuple[Mode, ...] = ("ro", "rw")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PATTERN_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9 ._*/-]*$")
CODE_PREFIX = "nm_"


class MemberError(Exception):
    """An admin operation was invalid; the message is safe to show."""


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str


@dataclass
class Grant:
    path: str
    mode: Mode


@dataclass
class Member:
    name: str
    created_at: str
    github_id: int | None = None
    github_login: str | None = None
    code_hash: str | None = None
    generation: int = 0
    grants: list[Grant] = field(default_factory=list)
    last_login: str | None = None

    def build_grants(self) -> Grants:
        return Grants.build(
            read=[g.path for g in self.grants if g.mode == "ro"],
            write=[g.path for g in self.grants if g.mode == "rw"],
        )


@dataclass(frozen=True)
class Principal:
    """The resolved caller of a tool: identity for commits plus access."""

    subject: str
    name: str
    email: str
    grants: Grants
    is_admin: bool


# Auth disabled (local dev): one implicit admin.
LOCAL = Principal(
    subject="local", name="local", email="local@notes-mcp.invalid", grants=FULL, is_admin=True
)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def _github_email(user_id: int, login: str) -> str:
    # GitHub's own noreply form; links commits to the account.
    return f"{user_id}+{login}@users.noreply.github.com"


def validate_pattern(path: str) -> str:
    path = path.strip().strip("/")
    if not path or not _PATTERN_RE.match(path) or ".." in path.split("/"):
        raise MemberError(
            f"invalid path pattern {path!r}: use repo-relative paths like "
            "kitchen, kitchen/**, or resources/recipes"
        )
    return path


class MemberStore:
    def __init__(self, path: Path, admin_logins: Iterable[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._members: dict[str, Member] = {}
        # admin login (lowercase) → pinned GitHub id (None until first login)
        self._admins: dict[str, int | None] = {}
        # admin login → admin-page session generation; signing out bumps it,
        # ending every admin-page session of theirs (MCP tokens unaffected)
        self._admin_sessions: dict[str, int] = {}
        self._load()
        configured = {login.strip().lower() for login in admin_logins if login.strip()}
        # Dropping a login from ADMIN_USERS drops its pin, which revokes it.
        self._admins = {login: self._admins.get(login) for login in configured}
        self._admin_sessions = {k: v for k, v in self._admin_sessions.items() if k in self._admins}

    # ------------------------------------------------------------------
    # persistence

    def _load(self) -> None:
        if not self.path.is_file():
            return
        raw = json.loads(self.path.read_text())
        self._admins = {k: v for k, v in raw.get("admins", {}).items()}
        self._admin_sessions = {k: int(v) for k, v in raw.get("admin_sessions", {}).items()}
        for data in raw.get("members", []):
            grants = [Grant(g["path"], g["mode"]) for g in data.pop("grants", [])]
            member = Member(**data, grants=grants)
            self._members[member.name] = member

    def _save(self) -> None:
        data = {
            "admins": self._admins,
            "admin_sessions": self._admin_sessions,
            "members": [asdict(m) for m in self._members.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # login

    def login_github(self, user: GitHubUser) -> str | None:
        """Subject for a GitHub user, or None if they aren't allowed."""
        with self._lock:
            if user.id in self._admins.values():
                return f"admin:{user.id}"
            login = user.login.lower()
            if login in self._admins and self._admins[login] is None:
                self._admins[login] = user.id  # ADMIN_USERS wins over membership
                self._save()
                return f"admin:{user.id}"
            for member in self._members.values():
                if member.github_id == user.id:
                    return f"member:{member.name}"
            return None

    def login_code(self, code: str) -> str | None:
        digest = _hash_code(code)
        with self._lock:
            for member in self._members.values():
                if member.code_hash and hmac.compare_digest(member.code_hash, digest):
                    return f"member:{member.name}"
        return None

    def generation(self, subject: str) -> int:
        kind, _, key = subject.partition(":")
        with self._lock:
            if kind == "member" and (member := self._members.get(key)):
                return member.generation
        return 0

    def record_login(self, subject: str) -> None:
        kind, _, key = subject.partition(":")
        with self._lock:
            if kind == "member" and (member := self._members.get(key)):
                member.last_login = _now()
                self._save()

    def resolve(self, subject: str, generation: int = 0) -> Principal | None:
        """The current principal for a token subject, or None if revoked."""
        kind, _, key = subject.partition(":")
        with self._lock:
            if kind == "admin":
                for login, pinned in self._admins.items():
                    if pinned is not None and str(pinned) == key:
                        return Principal(
                            subject=subject,
                            name=login,
                            email=_github_email(pinned, login),
                            grants=FULL,
                            is_admin=True,
                        )
                return None
            if kind == "member":
                member = self._members.get(key)
                if member is None or member.generation != generation:
                    return None
                email = (
                    _github_email(member.github_id, member.github_login)
                    if member.github_id is not None and member.github_login
                    else f"{member.name}@members.notes-mcp.invalid"
                )
                return Principal(
                    subject=subject,
                    name=member.name,
                    email=email,
                    grants=member.build_grants(),
                    is_admin=False,
                )
        return None

    def _admin_login(self, subject: str) -> str | None:
        kind, _, key = subject.partition(":")
        if kind != "admin":
            return None
        return next((k for k, v in self._admins.items() if v is not None and str(v) == key), None)

    def admin_session_generation(self, subject: str) -> int:
        with self._lock:
            login = self._admin_login(subject)
            return self._admin_sessions.get(login, 0) if login else 0

    def end_admin_sessions(self, subject: str) -> None:
        """Sign an admin out of the admin page everywhere."""
        with self._lock:
            if login := self._admin_login(subject):
                self._admin_sessions[login] = self._admin_sessions.get(login, 0) + 1
                self._save()

    def is_admin_subject(self, subject: str) -> bool:
        principal = self.resolve(subject)
        return principal is not None and principal.is_admin

    # ------------------------------------------------------------------
    # admin operations (the admin page is the only caller)

    def members(self) -> list[Member]:
        with self._lock:
            return sorted(self._members.values(), key=lambda m: m.name)

    def admins(self) -> dict[str, int | None]:
        with self._lock:
            return dict(self._admins)

    def _get(self, name: str) -> Member:
        member = self._members.get(name)
        if member is None:
            raise MemberError(f"no member named {name!r}")
        return member

    def _check_github_free(self, user: GitHubUser, except_name: str | None = None) -> None:
        if user.id in {pin for pin in self._admins.values() if pin is not None} or (
            user.login.lower() in self._admins
        ):
            raise MemberError(f"{user.login} is an admin already")
        for member in self._members.values():
            if member.github_id == user.id and member.name != except_name:
                raise MemberError(f"GitHub user {user.login} is already member {member.name}")

    def add_member(self, name: str, github: GitHubUser | None = None) -> Member:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            raise MemberError("name must be 1-32 lowercase letters, digits or hyphens")
        with self._lock:
            if name in self._members:
                raise MemberError(f"member {name!r} already exists")
            if github is not None:
                self._check_github_free(github)
            member = Member(name=name, created_at=_now())
            if github is not None:
                member.github_id, member.github_login = github.id, github.login
            self._members[name] = member
            self._save()
            return member

    def delete_member(self, name: str) -> None:
        with self._lock:
            self._get(name)
            del self._members[name]
            self._save()

    def set_github(self, name: str, github: GitHubUser | None) -> None:
        with self._lock:
            member = self._get(name)
            if github is not None:
                self._check_github_free(github, except_name=name)
                member.github_id, member.github_login = github.id, github.login
            else:
                member.github_id = member.github_login = None
            member.generation += 1
            self._save()

    def new_code(self, name: str) -> str:
        """Create or replace the member's code; returns it (shown once)."""
        code = CODE_PREFIX + secrets.token_urlsafe(24)
        with self._lock:
            member = self._get(name)
            member.code_hash = _hash_code(code)
            member.generation += 1
            self._save()
        return code

    def remove_code(self, name: str) -> None:
        with self._lock:
            member = self._get(name)
            member.code_hash = None
            member.generation += 1
            self._save()

    def revoke_sessions(self, name: str) -> None:
        with self._lock:
            self._get(name).generation += 1
            self._save()

    def set_grant(self, name: str, path: str, mode: str) -> None:
        valid: dict[str, Mode] = {"ro": "ro", "rw": "rw"}
        if mode not in valid:
            raise MemberError("mode must be ro or rw")
        path = validate_pattern(path)
        with self._lock:
            member = self._get(name)
            member.grants = [g for g in member.grants if g.path != path]
            member.grants.append(Grant(path, valid[mode]))
            member.grants.sort(key=lambda g: g.path)
            self._save()

    def set_grants(self, name: str, changes: dict[str, str]) -> int:
        """Apply the folder picker: path → "ro", "rw" or "none", all at once.

        "kitchen" and "kitchen/**" mean the same, so setting either replaces
        both. Paths not in `changes` keep their grants. Returns how many
        grants changed.
        """
        valid: dict[str, Mode] = {"ro": "ro", "rw": "rw"}
        cleaned: dict[str, Mode | None] = {}
        for raw, mode in changes.items():
            if mode not in (*valid, "none"):
                raise MemberError("mode must be none, ro or rw")
            cleaned[validate_pattern(raw).removesuffix("/**")] = valid.get(mode)
        with self._lock:
            member = self._get(name)
            before = {g.path: g.mode for g in member.grants}
            kept = [g for g in member.grants if g.path.removesuffix("/**") not in cleaned]
            added = [Grant(path, mode) for path, mode in cleaned.items() if mode is not None]
            member.grants = sorted(kept + added, key=lambda g: g.path)
            after = {g.path: g.mode for g in member.grants}
            changed = sum(before.get(p) != after.get(p) for p in before.keys() | after.keys())
            if changed:
                self._save()
            return changed

    def remove_grant(self, name: str, path: str) -> None:
        with self._lock:
            member = self._get(name)
            member.grants = [g for g in member.grants if g.path != path]
            self._save()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "admins": len(self._admins),
                "members": len(self._members),
            }
