# notes-mcp — Product & Design Document

A self-hosted remote MCP server that exposes a personal notes git repository
to Claude via the mobile/web app: list, read, search, create, append to, and
edit notes; store files; execute scripts inside note folders. Every write is
a git commit pushed to the repo's remote. Designed for one owner who can
share chosen folders with a few others such as a household, running one
container on a home server, exposed through a Cloudflare Tunnel.

- Operational how-to (dev loop, container, deploy): `README.md`, `deploy/README.md`
- Configuration reference: `.env.example` (every variable, commented)

## Goals and non-goals

**Goals (v1):** read/write/search a notes repo from the Claude app; file
uploads into notes; script execution with streaming progress; every change a
git commit with a distinct author; OAuth that actually works with the Claude
connector; survives unattended on a NAS.

**Non-goals (deferred to v2):** async/job-based script execution; a
status/dashboard endpoint; a webapp UI; writing scripts via MCP
(model-generated executable code committed to the repo); per-member script
sandboxes (members can't run scripts); web uploads of large binaries (#3);
note deletion via MCP; wiki-style backlink management; committing script
outputs; PR-based conflict handling; the **semantic layer** — staged plan:
the `index: true` note-frontmatter convention and the resource class exist
now so curation accumulates; stage 1 is SQLite FTS5/BM25 over everything
(trigger: lexical search visibly missing); stage 2 is local embeddings
(sqlite-vec, small ONNX model on the host) over resources by default plus
opted-in notes, chunked by heading (trigger: conceptual queries failing
against accumulated reference libraries) — API embeddings are rejected as
contrary to the third-party-free direction; a **third-party-free profile** — direct
TLS ingress (own reverse proxy + ACME instead of a Cloudflare Tunnel, which
sees plaintext), a self-hosted git remote (bare repo/Forgejo instead of
GitHub, needs SSH support in git_ops), and a non-GitHub identity step
(passkey/WebAuthn page; the provider's `github_exchange` seam is where it
swaps in). Note the model provider still sees note content — "third-party-
free" can only cover infrastructure.

## Architecture

```
Claude app (phone/web)
      │  HTTPS + OAuth 2.1 (this server is the authorization server;
      │  GitHub is the upstream identity)
      ▼
Cloudflare edge — mcp.<your-domain> (proxied CNAME → tunnel)
      │  Cloudflare Tunnel (outbound-only from the host; no inbound ports)
      ▼
cloudflared container ──┐ shares notes-mcp's network namespace;
                        │ tunnel ingress targets http://localhost:8000
notes-mcp container ────┘ (Docker default bridge)
      ├── /repo   volume — working clone of the notes repo
      ├── /data   volume — OAuth state (JWT signing secret, client registrations)
      └── outbound HTTPS to GitHub (git pull/push, PAT via GIT_ASKPASS)
```

The tunnel is ingress only — there is deliberately **no Cloudflare Access
application** in front of `/mcp` (see [Design decisions](#design-decisions)).

## Note model (hybrid)

Two kinds of note coexist, so both flat file-per-note repos (GitJournal,
Obsidian-style) and folder-per-note layouts work:

- **File note** — any `.md` file; id is its repo-relative path
  (`topic/idea.md`). No attached files or scripts.
- **Folder note** — any directory containing `index.md`; id is the directory
  path (`cycling-analysis`). May hold arbitrary data files and a `scripts/`
  directory. The only kind `add_file_to_note` / `run_script` accept.

Rules:

- Note ids are repo-relative paths; each segment must match
  `[A-Za-z0-9][A-Za-z0-9 ._-]*` (no dotfiles, no traversal, no absolute
  paths). `foo/index.md` is rejected as an id — address the folder.
- Title resolution: frontmatter `title` → first H1 → filename stem / folder
  name. Frontmatter is entirely optional.
- `updated_at` comes from `git log -1 --format=%cI` per note, cached;
  invalidated on writes, fully refreshed after every pull. (File mtimes are
  useless after a fresh clone.)
- Hidden directories (`.templates`, `.mcp`, `.git*`) and `scripts/` dirs are
  never notes.
- `create_note` picks the kind from the id: trailing `.md` → file note,
  otherwise folder note with a templated `index.md`.

Templates live in `<repo>/.templates/*.md` with `{{title}}`, `{{tags}}`,
`{{created}}`, `{{note_id}}` placeholders (plain substitution). A built-in
default is compiled into the server so the repo needs no bootstrap.

## Resources (imported reference documents)

Resources are a second content class, deliberately distinct from notes:

|                         | Note                               | Resource                                   |
| ----------------------- | ---------------------------------- | ------------------------------------------ |
| Origin                  | authored by the user               | imported (article, spec, paper, book)      |
| Lifecycle               | living — append/edit/rewrite       | replaced wholesale on re-import            |
| In `list_notes`         | yes                                | no — `list_resources` or search            |
| Read pattern            | whole (notes are small)            | ranged windows, never whole                |
| Semantic index (future) | opt-in (`index: true` frontmatter) | on by default — findability is the point   |
| Metadata                | title/tags                         | `source` URL, `retrieved` date, `fidelity` |

They live under a top-level `resources/<topic>/<slug>.md`, text-only by
construction (`add_resource` accepts strings; convert documents before
import). Unified lexical search covers them — `search_notes` hits inside
`resources/` return a `resource_id` instead of a `note_id`.

**Ingestion honesty:** a model cannot faithfully reproduce a long document
in one tool call, so transfers are chunked (`add_resource` create, then
`append: true` calls; responses report `total_lines` for verification) and
every resource declares `fidelity: verbatim` or `fidelity: digest` in
frontmatter. Book-scale material (> ~5k lines) is imported by committing
directly to the repo from a desktop — the chat path refuses to pretend.
The bundled **resource-ingestion skill** (`skills/resource-ingestion/`)
encodes this workflow for the Claude client; imported documents skip the
prettier formatting step so they stay verbatim.

## Tool surface (v1 — complete)

All tools return JSON objects; expected failures return
`{"error": <category>, "details": {...}}` rather than raising. Error
categories: `invalid_note_id`, `note_not_found`, `note_already_exists`,
`invalid_template`, `template_not_found`, `no_match`, `multiple_matches`,
`invalid_filename`, `not_a_folder_note`, `file_too_large`,
`invalid_script_name`, `script_not_found`, `unrecognized_interpreter`,
`timeout`, `merge_conflict`, `push_failed`, `git_error`, `search_failed`,
`file_not_found`, `invalid_commit`, `restore_failed`, `invalid_content`,
`invalid_resource_id`, `resource_not_found`, `internal_error`.

| Tool                                                        | Returns                                                                             | Notes                                                                                          |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `list_notes(filter?)`                                       | `{notes: [{id, title, tags, updated_at, path}]}`                                    | newest first; filter is a case-insensitive substring over id/title/tags                        |
| `read_note(note_id)`                                        | `{content, files: [{name, size, type}], frontmatter}`                               | `files` empty for file notes; excludes nested notes' files                                     |
| `search_notes(query, max_results=20)`                       | `{results: [{note_id, file, line_number, snippet}]}`                                | ripgrep, literal (`--fixed-strings`), smart-case, ±2 context lines, binaries skipped           |
| `create_note(note_id, title, tags?, template?)`             | `{status, note_id, path, git}`                                                      | kind from id suffix; fails if exists                                                           |
| `append_to_note(note_id, content, section?)`                | `{status, note_id, appended_to, git}`                                               | section matched case-insensitively at any heading level, fence-aware; created at EOF if absent |
| `edit_note(note_id, old_str, new_str)`                      | `{status, note_id, git}`                                                            | exactly one occurrence or structured error                                                     |
| `add_file_to_note(note_id, filename, content_b64)`          | `{status, filename, size, overwritten, git}`                                        | folder notes only; bare filename; ≤20 MB; overwrites (flagged)                                 |
| `list_scripts(note_id)`                                     | `{scripts: [{name, description}]}`                                                  | description = docstring or top comment, ≤200 chars                                             |
| `run_script(note_id, script_name, args?, timeout_seconds?)` | `{exit_code, stdout, stderr, duration_seconds, stdout_truncated, stderr_truncated}` | see below                                                                                      |

| `read_note_file(note_id, filename, start_line?, limit=500)` | `{content or content_b64, encoding, size, type}` | folder notes; ranged reads for large text files; NUL-byte binary detection |
| `write_note(note_id, content)` | `{status, note_id, bytes, git}` | full-body replace; old version stays in history |
| `move_note(note_id, new_note_id)` | `{status, from, to, git}` | same-kind only; folder notes move with all files; links not rewritten |
| `note_history(note_id, limit=10)` | `{history: [{commit, author, date, message}]}` | newest first |
| `restore_note(note_id, commit)` | `{status, restored_from, git}` | restores the markdown body as a NEW commit — undo that is itself undoable |
| `list_resources()` | `{resources: [{id, title, source, fidelity, retrieved, size, updated_at}]}` | imported documents, not notes |
| `add_resource(resource_id, content, append=false)` | `{status, resource_id, size, total_lines, git}` | chunked transfer via append; replaces wholesale without it; skips prettier |
| `read_resource(resource_id, start_line=1, limit=200)` | `{content, start_line, end_line, total_lines}` | ranged only — a book never comes back whole |

Write-tool responses carry `git: {committed, pushed, commit, formatted}`.

### Script execution

- Interpreter from shebang, else extension (`.py` → python3, `.sh` → bash);
  otherwise `unrecognized_interpreter`.
- cwd = the note folder. Timeout default 60 s, hard max 300 s
  (`SCRIPT_TIMEOUT_*`); on timeout the whole process group is SIGKILLed
  (`start_new_session=True`), partial output returned in the error details.
- Output caps: 1 MB stdout / 256 KB stderr — exceeding a cap truncates
  (flagged) but does not kill the script.
- Keepalives: every 15 s the server emits an MCP log notification plus a
  progress notification (latest stdout line rides along), so Cloudflare's
  ~100 s idle timeout can't drop a quiet run. Log notifications flow even
  when the client sends no progressToken.
- Script writes to the note folder stay uncommitted; the startup dirty-tree
  reset treats them as scratch.
- Every execution logs one structured JSON line (script, exit code, duration,
  byte counts).

## Git workflow

Every write tool runs, under one process-wide asyncio lock (single user ≠
single in-flight request):

1. `git pull --rebase origin <branch>` — conflict → abort rebase, return
   `merge_conflict` (resolve on a desktop clone, by design)
2. mutate the working tree
3. `prettier --write` on the changed `.md` (best-effort; see design decisions)
4. `git add` + `git commit` with the configured author/committer identity
   (`GIT_AUTHOR_NAME`/`EMAIL`) — messages: `Create note: <id>` /
   `Append to <id>` / `Edit <id>` / `Add <file> to <id>`
5. `git push`; on rejection: pull --rebase once and retry; second failure →
   `push_failed` (commit stays local)

`run_script` holds the same lock — a script mutating its folder while another
call rebases would corrupt the tree. Consequence: a long script blocks writes
(bounded at 300 s).

Startup sequence: validate env → if `NOTES_REPO_PATH` has no `.git`, clone
`NOTES_REPO_URL` (refusing to clobber a non-empty non-git dir) → if the tree
is dirty, log `git status`, hard-reset + clean → pull. Clone/pull failure
exits non-zero; Docker's restart policy retries, so a bad token or URL
surfaces quickly in the logs. Git credentials are supplied through a
generated `GIT_ASKPASS` script reading a `GIT_PASSWORD` env var set only on
git subprocesses — the PAT never appears in `.git/config`, on disk, or in
the remote URL. The distinct commit author keeps the server's commits
greppable next to your own.

## Authentication

**This server is the OAuth 2.1 authorization server.** Two kinds of
principal (`members.py`):

- **Admins:** GitHub users in `ADMIN_USERS`. Full access, scripts, and the
  admin page.
- **Members:** created on the admin page. Each signs in with GitHub and/or
  an access code and has per-path grants.

Flow: the Claude connector registers via Dynamic Client Registration →
`/authorize` validates the client and sends the browser to our login page
(`/auth/login`) → the person picks **Sign in with GitHub** (→ GitHub →
`/auth/callback`) or enters an **access code** (→ `/auth/code`) → the server
maps that to a principal or refuses (403) → issues its own authorization
code → the client exchanges it at `/token` (PKCE S256 verified) for tokens.

- GitHub people are matched by **numeric account id, never username**.
  Usernames can be renamed and then re-registered by someone else. Admins'
  ids are pinned at first sign-in; members' ids are resolved when an admin
  adds them by username.
- Access codes are 192-bit random (`nm_…`), stored only as SHA-256
  hashes, and shown to the admin once. Five wrong codes end the pending
  login.
- The MCP SDK provides the endpoints:
  `/.well-known/oauth-protected-resource/mcp`,
  `/.well-known/oauth-authorization-server`, `/register`, `/authorize`,
  `/token`, and the 401 with
  `WWW-Authenticate: Bearer ... resource_metadata="..."` on `/mcp` that the
  Claude connector requires for discovery.
- Tokens are stateless HS256 JWTs (`iss` = PUBLIC_URL, `aud` = notes-mcp,
  `sub` = `admin:<github id>` or `member:<name>`, `gen` = the member's
  generation): access 1 h, refresh 30 d, rotated on refresh.
- **Every use re-resolves the principal**, so revocation is immediate:
  deleting a member, replacing or removing their code, relinking their
  GitHub, or "End sessions" bumps the generation and kills every outstanding
  token. Dropping a name from `ADMIN_USERS` (restart) revokes that admin.
  The revoke endpoint is a no-op. Rotating the signing secret (delete
  `<state>/signing_secret`, restart) force-expires everyone.
- Grants are **not** in tokens. Editing a member's grants applies on their
  next tool call, with no new token.
- Signing secret, client registrations and members (`members.json`, 0600)
  persist in `OAUTH_STATE_DIR` (the `/data` volume). Auth codes and pending
  logins are in-memory (minutes).
- Writes are committed with the calling principal as git author: an admin
  as `<id>+<login>@users.noreply.github.com`, a member by GitHub noreply or
  `<name>@members.notes-mcp.invalid`. The configured `GIT_AUTHOR_*` identity
  is the committer. The caller is read from the HTTP request on each MCP
  message, not the SDK's auth contextvar, which a long-lived session task
  can carry stale.
- Auth is enabled iff `PUBLIC_URL` is set. Without it the server runs open
  for local dev (one implicit admin) and logs a warning. With it, the
  server refuses to start unless the repo is a git repository: members are
  promised recoverable, attributed writes. `GET /health` is always public.
- The GitHub OAuth app needs callback `<PUBLIC_URL>/auth/callback`, device
  flow off. The server requests only the `read:user` scope. Admin sign-in
  reuses the same callback.

### Admin page (`/admin`)

Server-rendered HTML forms, no scripts, for ADMIN_USERS. Actions: add or
delete members, link or unlink a GitHub account, create, replace or remove
an access code, end sessions, and grant a path read-only or read-write
(suggestions list the repo's folders; any pattern is accepted).

- **Not an MCP tool, on purpose.** The model can be steered by the content
  it reads; who-may-see-what changes only through a page a human operates.
- **Session:** an `admin_session` JWT in a cookie (HttpOnly, SameSite=Lax,
  Secure on https, Path=/admin, 8 h), re-checked against ADMIN_USERS on
  every request. MCP bearer tokens are never accepted.
- **Requests:** every POST carries a CSRF token bound to the session, and
  a cross-origin `Origin` is refused.
- **Headers:** CSP `default-src 'none'` (inline styles only),
  `frame-ancestors 'none'`, no-store.
- **Optional extra lock:** put Cloudflare Access in front of `/admin*` only.
  Access breaks the MCP connector, not browser pages.

## Access control

Members' grants are repo-relative path patterns, each `ro` or `rw` (globs;
resources are `resources/<id>`; a pattern covers everything beneath what it
names; rw implies read). A member with no grants sees nothing. Policy lives
in server state edited by admins, never in note content: sharing by
frontmatter would let a prompt-injected model share notes by editing them.

Enforcement has one choke point. Every tool is wrapped by `guarded(...)`,
which names the arguments that are paths and whether they are read or
written; registration fails at import if any tool lacks a declaration.

- Unreadable paths answer exactly like missing ones (`note_not_found`,
  `resource_not_found`): no existence leak. Unwritable paths answer
  `forbidden` whether or not they exist.
- A request that carries no resolvable principal while auth is on sees
  nothing (fail closed).
- list/search filter their output per item; search filters before the
  `max_results` cap so hidden hits can't crowd out visible ones.
- Paths are also checked where they really point, so a symlink committed
  into a shared folder doesn't carry the share.
- `note_history` hides the message of any commit that also touched a path
  the reader can't see (messages name paths, e.g. "Move a to b").
- `run_script` is admin-only, even for a member granted `**`: a script reads
  the whole checkout, and a member could upload one into a folder they can
  write. A per-member filtered checkout could lift this later.
- Grants are path-based, so a path's whole history belongs to whoever can
  read the path today, including content from before it was shared.
- **Members can't destroy anything permanently.** Every member write is a
  git commit, nothing rewrites history (no force-push, no rebase of pushed
  commits), there is no delete tool, and attachments are ordinary git blobs
  (no LFS; see #2). Anything a member changes can be restored from history.
- **Assurance** is a canary sweep (`tests/test_access.py`), not a proof.
  Private content carries a marker. A read-write and a read-only member each
  call every registered tool with hostile arguments: traversal, prefix
  tricks, private commit shas, moves out of and into private areas. After
  each call, no response may contain the marker, no file outside their
  write grants may change, the tree must be clean, and HEAD must still
  descend from the original commit (a read-only member must not move it).
  Removing or weakening any single guard, token check, or admin-page check
  makes some test fail (mutation-checked).
- **Considered and deferred: Cedar** (a policy language with a
  Lean-verified analyzer). Its proofs cover the policy, not whether the
  code consults it, which is where the risk is, and path globs don't need
  its expressiveness. The decision is one `Grants` object, so swapping in a
  policy engine later is local.

## Container & security model

- Base `python:3.14-slim` + git, ripgrep, nodejs/npm (`prettier@3`).
  Non-root user (UID 1000). Named volumes only — no host paths, no Docker
  socket, not privileged.
- `--memory 2g`. No `--cpus` (unsupported on Synology DSM kernels); script
  timeouts bound CPU abuse.
- Arbitrary code execution is accepted in v1: the container is the sandbox,
  admins only (members can't run scripts), git is audit and recovery.
- Secrets are env vars (`GITHUB_TOKEN`, `GITHUB_OAUTH_CLIENT_SECRET`), each
  with a `_FILE` variant for Docker-secret setups. Accepted trade-off: env
  vars are visible in `docker inspect`.
- `HEALTHCHECK` hits `/health` (200 iff the working tree is readable);
  `restart: unless-stopped` on both containers.
- Structured JSON logs to stdout — every tool call carries `tool`, `note_id`,
  `duration_ms`, `status`, `request_id`; the host handles retention.
- The git PAT should be fine-grained, scoped to the notes repo only,
  Contents read/write.

## Deployment

See **`deploy/README.md`** for the full runbook (GitHub OAuth app, PAT,
tunnel setup, the deploy script, Synology gotchas, troubleshooting).
Summary: build image → `docker save` → copy to the host →
`sh deploy.sh <env-file> <tarball>`. The script is generic (everything comes
from the env file) and idempotent; updates re-run it, and the volumes
(repo clone, OAuth state) persist so updates don't force reconnecting the
Claude app.

Continuous deploy is pull-based: the Deploy workflow promotes a tag to
`:prod`; a scheduled poller on the host redeploys on digest change with a
`/health` gate. Rollback = promote an older `sha-…` tag.

## Design decisions

Rationale for the choices most likely to be questioned:

1. **The server does its own OAuth instead of delegating to Cloudflare
   Access Managed OAuth.** Access returns its 401 without the
   `WWW-Authenticate` header, and the claude.ai web/mobile connector dies at
   the Connect button against it (anthropics/claude-ai-mcp#410, closed
   not-planned; Claude Code tolerates the missing header, the app does not).
   Running a spec-correct OAuth server in-process — with the MCP SDK doing
   the heavy lifting — is the reliable path. The tunnel remains for ingress.
2. **Hybrid note model.** Real notes repos are usually flat `.md` files;
   requiring folder-per-note would demand a repo migration. Folder notes are
   kept as the container for data files and scripts.
3. **Markdown formatting in the write flow.** Repo-side pre-commit hooks
   never fire for server-side commits (fresh clones don't set
   `core.hooksPath`), so the server runs prettier between mutate and commit.
   Best-effort: a missing/failing formatter logs and commits unformatted —
   a formatting problem must never eat a note edit.
4. **Shared network namespace instead of a custom bridge.** Synology's DSM
   firewall silently drops egress from CLI-created custom docker networks.
   cloudflared joins the server's namespace and targets `localhost:8000` —
   simpler and portable. Corollary: recreating the server container requires
   recreating cloudflared (the deploy script always does both).
5. **Conflicts abort, they are not auto-resolved.** A `merge_conflict` error
   is returned to the model, which reports it; resolution happens on a
   desktop clone. Auto-resolution or PR flows are v2 questions.
6. **Search is literal** (`rg --fixed-strings`): queries come from chat, and
   a stray `(` must not become a regex error.
7. **Tool results are top-level JSON objects** (`{"notes": [...]}`, not bare
   arrays) so successes and structured errors share a shape.
8. **Stateless JWTs over a token store.** Restart-safe with only a persisted
   signing secret; revocation-by-rotation is acceptable for a single user.
9. **Synchronous dispatch, one write lock.** Simple and correct for one
   user; async job queues are v2. The known cost: a long-running script
   blocks writes for up to its timeout.
10. **Secrets as env vars** (with `_FILE` variants). Visible in
    `docker inspect`, accepted for a single-admin host; flip to `_FILE` for
    Docker-secret setups without code changes.

## Tech stack

Python 3.14 · official `mcp` SDK (FastMCP, streamable HTTP transport) ·
subprocess git (not GitPython) · PyJWT · httpx · uv · ruff + ty + pytest
(CI in `.github/workflows/ci.yml`) · `python:3.14-slim` container with
git/ripgrep/prettier.
