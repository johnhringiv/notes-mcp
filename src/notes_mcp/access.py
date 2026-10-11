"""Path grants: which repo paths a principal may read and write.

Patterns are glob-matched against repo-relative paths (`*` within one path
segment, `**` across segments). A pattern also covers everything beneath
what it names, so "kitchen" and "kitchen/**" both grant the folder and its
contents. Resources are addressed as "resources/<resource_id>". Write
implies read.

Who holds which grants is members.py's job; this module only matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


def _expand(pattern: str) -> tuple[str, ...]:
    pattern = pattern.strip().strip("/")
    if pattern in ("", "**"):
        return ("**",)
    base = pattern.removesuffix("/**")
    return (base, f"{base}/**")


@dataclass(frozen=True)
class Grants:
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    @classmethod
    def build(cls, read: list[str], write: list[str]) -> Grants:
        expanded_write = tuple(p for raw in write for p in _expand(raw))
        expanded_read = tuple(p for raw in read for p in _expand(raw))
        return cls(read=expanded_read + expanded_write, write=expanded_write)

    @staticmethod
    def _matches(path: str, patterns: tuple[str, ...]) -> bool:
        parts = path.split("/")
        if not path or path.startswith("/") or any(seg in ("", ".", "..") for seg in parts):
            return False  # never let an unnormalized path glob its way out
        if "**" in patterns:
            return True
        candidate = PurePosixPath(path)
        return any(candidate.full_match(p) for p in patterns)

    def can_read(self, path: str) -> bool:
        return self._matches(path, self.read)

    def can_write(self, path: str) -> bool:
        return self._matches(path, self.write)


FULL = Grants.build(read=[], write=["**"])
NONE = Grants()
