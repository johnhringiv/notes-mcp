# notes-mcp

Remote MCP server exposing a personal notes git repo to Claude via the
mobile/web app. Notes are markdown files (or folders with `index.md`, data
files, and runnable scripts); every write is a prettier-formatted git commit
pushed to GitHub. Auth is OAuth 2.1 served by this server with GitHub as the
login identity. Single-user by design: exactly one GitHub login is allowed
through; everyone else gets a 403.

| Doc                                    | Contents                                                                                                                         |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [`PRD.md`](PRD.md)                     | The system as built: architecture, note model, tool surface, git workflow, auth design, security model, decision log, v2 roadmap |
| [`deploy/README.md`](deploy/README.md) | Script-based deploys, continuous deploy, Synology/DSM specifics, troubleshooting                                                 |
| [`.env.example`](.env.example)         | Every configuration variable, commented                                                                                          |

## Self-hosting

### What you need

- **A GitHub account.** GitHub is load-bearing three ways: it hosts the
  notes repo, a fine-grained PAT is the push credential, and a GitHub OAuth
  app is the login identity. Swapping in GitLab or a self-hosted remote is
  not supported without code changes (see the v2 roadmap in the PRD).
- **A notes repo.** A private GitHub repo with at least one commit on the
  branch you'll track (a repo created with a README is fine — see
  [Your notes repo](#your-notes-repo)).
- **A Docker host** with compose. Any always-on Linux box works.
  (Synology/DSM users: use the script path in
  [`deploy/README.md`](deploy/README.md) instead of compose.)
- **HTTPS ingress.** The worked path below uses a Cloudflare Tunnel (needs a
  domain on Cloudflare; no inbound ports on your host). Any HTTPS reverse
  proxy works instead — see
  [Bring your own reverse proxy](#bring-your-own-reverse-proxy).

Pick your public hostname now (e.g. `mcp.example.com`) — steps 1 and 3
reference it.

### 1. GitHub OAuth app (login identity)

github.com → Settings → Developer settings → **OAuth Apps** → New OAuth App
(not a GitHub App):

- Homepage: `https://<your-hostname>`
- Authorization callback URL: `https://<your-hostname>/auth/callback` (exact)
- Leave "Enable Device Flow" unchecked
- Save the **Client ID** (public) and generate a **client secret** (shown once)

### 2. Fine-grained PAT (git push credential)

github.com → Settings → Developer settings → Personal access tokens →
**Fine-grained tokens**:

- Repository access: _Only select repositories_ → your notes repo
- Permissions → Repository → **Contents: Read and write** (nothing else)
- When it expires, pushes fail with `push_failed`/`git_error` in the logs —
  rotate it in `.env` and restart.

### 3. Cloudflare Tunnel (ingress)

Zone must be on Cloudflare. Zero Trust dashboard → Networks → Tunnels →
Create → Cloudflared; copy the **tunnel token**; add public hostname
`<your-hostname>` → service `http://notes-mcp:8000` (the compose service —
cloudflared reaches it by container name).

Do **not** put a Cloudflare Access application in front of `/mcp` — the
server does its own OAuth, and Access breaks the Claude connector (see PRD
decision log). DNS: the dashboard creates the proxied CNAME; an existing
wildcard record doesn't conflict.

### 4. Configure

```sh
git clone https://github.com/johnhringiv/notes-mcp.git && cd notes-mcp
cp .env.example .env && chmod 600 .env
```

Fill in `.env`: `NOTES_REPO_URL`, `GITHUB_TOKEN` (the PAT), `PUBLIC_URL`
(`https://<your-hostname>`), `GITHUB_OAUTH_CLIENT_ID`,
`GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_ALLOWED_LOGIN` (your GitHub username),
and `TUNNEL_TOKEN`. Every variable is documented in the file.

### 5. Run

```sh
docker compose up -d
```

This pulls the public image `ghcr.io/johnhringiv/notes-mcp:latest` — no
build step, no registry auth. (Forks get their own: CI publishes to the
fork's GHCR on every push to main. Set `IMAGE=` in `.env` to pin a release
tag like `:0.2.0` or a `sha-…` tag, or to point at a fork.)

### 6. Verify

```sh
curl https://<your-hostname>/health               # {"status":"ok","version":"…"}
curl -i -X POST https://<your-hostname>/mcp | head -3   # 401 + WWW-Authenticate
```

If either fails, see the troubleshooting table in
[`deploy/README.md`](deploy/README.md#troubleshooting).

### 7. Connect Claude

In the Claude app (web or mobile): Settings → **Connectors** → **Add custom
connector** → URL `https://<your-hostname>/mcp`. Claude sends you through
the GitHub login; approve, and you're connected. A successful connect logs
`registered client` then `authorization granted` in
`docker logs notes-mcp`; a login by any other GitHub user is rejected with
a JSON error naming the login it saw.

### Your notes repo

Start with any repo — a fresh one containing only a README is fine (the
tracked branch must have at least one commit for the startup clone); the
server creates notes as you go. Two kinds of note coexist, so flat
Obsidian/GitJournal-style repos work as-is:

- **File note** — any `.md` file; its id is the repo-relative path
  (`topic/idea.md`).
- **Folder note** — any directory with an `index.md`; may hold data files
  and a `scripts/` directory that `run_script` executes.

Frontmatter is optional (title falls back to first H1, then filename).
Optional `create_note` templates live in `.templates/*.md`; a built-in
default is compiled in, so no bootstrap is required. Imported reference
documents (articles, papers) go under `resources/` — a separate content
class from notes; see the PRD.

### Bring your own reverse proxy

Cloudflare is ingress, not architecture: the server just needs `PUBLIC_URL`
to match whatever terminates TLS in front of it (Caddy, Traefik, nginx +
Let's Encrypt, Tailscale Serve, …). Don't edit `docker-compose.yml` — set
`COMPOSE_PROFILES=` (empty) in `.env` to drop the cloudflared sidecar, and
publish the port with a `docker-compose.override.yml` (compose merges it
automatically):

```yaml
services:
  mcp-server:
    ports:
      - "127.0.0.1:8000:8000"
```

Then proxy `https://<your-hostname>` → `http://127.0.0.1:8000`. The MCP
endpoint streams (streamable HTTP), so disable response buffering
(nginx: `proxy_buffering off`). Auth is unaffected — the server remains its
own OAuth issuer either way.

### Updating

```sh
docker compose pull && docker compose up -d
```

The `notes-repo` and `oauth-state` volumes persist, so updates keep the
clone, issued tokens, and the Claude connector registration. Pin a release
tag (`:0.2.0`) or `sha-…` tag via `IMAGE=` in `.env` for rollbacks. For unattended pull-based deploys
(poll a promoted `:prod` tag on a schedule), see
[`deploy/README.md`](deploy/README.md#continuous-deploy).

## Development

```bash
uv sync                                  # deps (Python 3.14, uv-managed)
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check

# run locally against any notes working tree (git optional; push needs origin)
NOTES_REPO_PATH=~/notes MCP_PORT=8000 uv run notes-mcp
```

Requires `git` and `rg` (ripgrep) on PATH; `prettier` optional locally
(writes commit unformatted without it). The MCP endpoint is
`http://host:port/mcp` (streamable HTTP); `GET /health` is a public liveness
probe reporting the running version. CI (`.github/workflows/ci.yml`) runs
ruff, ty, and pytest, then publishes the container image to GHCR.

One-time after cloning: `git config core.hooksPath .githooks` — enables the
pre-commit hook that prettier-formats staged markdown.

Local-dev degraded modes (production uses the full path):

- `NOTES_REPO_PATH` not a git repo → filesystem only, no commits
- git repo without an `origin` remote → commits locally, skips pull/push
- `PUBLIC_URL` unset → auth disabled (logged warning)

Local image build: `docker compose build` (or `docker build -t notes-mcp:dev .`).
For air-gapped hosts there's a `docker save` tarball path in
[`deploy/README.md`](deploy/README.md).

## Layout

```
src/notes_mcp/
├── server.py      # FastMCP app, tool registration, write-lock, /health, OAuth callback
├── auth.py        # OAuth 2.1 provider (GitHub upstream identity, JWT minting)
├── config.py      # env loading/validation (secrets: NAME or NAME_FILE)
├── git_ops.py     # pull/commit/push wrapper, clone-at-startup, write_flow
├── notes.py       # note model: list/read/search/create/append/edit
├── resources.py   # imported reference documents (add/list/read ranged)
├── files.py       # add_file_to_note
├── scripts.py     # list_scripts, run_script (timeouts, caps, keepalives)
├── formatting.py  # prettier on changed markdown before commit
├── templates.py   # create_note templates
├── logging.py     # structured JSON logging + tool-call wrapper
└── errors.py      # structured error helper
tests/             # unit + full OAuth flow + git integration
```
