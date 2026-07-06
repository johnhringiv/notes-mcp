# notes-mcp

Remote MCP server exposing a personal notes git repo to Claude via the
mobile/web app. Notes are markdown files (or folders with `index.md`, data
files, and runnable scripts); every write is a prettier-formatted git commit
pushed to GitHub. Auth is OAuth 2.1 served by this server with GitHub as the
login identity, fronted by a Cloudflare Tunnel.

| Doc                                    | Contents                                                                                                                         |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| [`PRD.md`](PRD.md)                     | The system as built: architecture, note model, tool surface, git workflow, auth design, security model, decision log, v2 roadmap |
| [`deploy/README.md`](deploy/README.md) | Deploy/update runbook: GitHub OAuth app + PAT + tunnel setup, Synology gotchas, troubleshooting                                  |
| [`.env.example`](.env.example)         | Every configuration variable, commented                                                                                          |

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
probe. CI (`.github/workflows/ci.yml`) runs ruff, ty, and pytest.

One-time after cloning: `git config core.hooksPath .githooks` — enables the
pre-commit hook that prettier-formats staged markdown.

Local-dev degraded modes (production uses the full path):

- `NOTES_REPO_PATH` not a git repo → filesystem only, no commits
- git repo without an `origin` remote → commits locally, skips pull/push
- `PUBLIC_URL` unset → auth disabled (logged warning)

## Container

```bash
docker build -t notes-mcp:<ver> .
docker save notes-mcp:<ver> | gzip > notes-mcp-<ver>.tar.gz
```

Then follow [`deploy/README.md`](deploy/README.md) — in short:
`sh deploy/deploy.sh <env-file> <tarball>` runs the server plus a
`cloudflared` sidecar sharing its network namespace. `docker-compose.yml`
encodes the same shape declaratively if you prefer compose.

## Layout

```
src/notes_mcp/
├── server.py      # FastMCP app, tool registration, write-lock, /health, OAuth callback
├── auth.py        # OAuth 2.1 provider (GitHub upstream identity, JWT minting)
├── config.py      # env loading/validation (secrets: NAME or NAME_FILE)
├── git_ops.py     # pull/commit/push wrapper, clone-at-startup, write_flow
├── notes.py       # note model: list/read/search/create/append/edit
├── files.py       # add_file_to_note
├── scripts.py     # list_scripts, run_script (timeouts, caps, keepalives)
├── formatting.py  # prettier on changed markdown before commit
├── templates.py   # create_note templates
├── logging.py     # structured JSON logging + tool-call wrapper
└── errors.py      # structured error helper
tests/             # unit + full OAuth flow + git integration
```
