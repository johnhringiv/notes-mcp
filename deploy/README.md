# Deployment runbook

How to stand up (or update) notes-mcp behind a Cloudflare Tunnel, with
Synology (DSM) specifics called out where they bite.

## One-time setup

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
  rotate it in the env file and re-run the deploy script.

### 3. Cloudflare Tunnel

Zone must be on Cloudflare. Either via the Zero Trust dashboard
(Networks → Tunnels → Create → Cloudflared; copy the token; add public
hostname `<host>` → `http://localhost:8000`) or via API. Do **not** put a
Cloudflare Access application in front of `/mcp` — the server does its own
OAuth, and Access breaks the Claude connector (see PRD decision log).

The ingress target is `http://localhost:8000` because cloudflared runs in
the server container's network namespace (see below).

DNS: the dashboard/API creates the proxied CNAME
`<host> → <tunnel-id>.cfargotunnel.com`. An existing wildcard record doesn't
conflict — the explicit record wins.

### 4. Env file

Copy `.env.example` → e.g. `notes-mcp.env`, fill in: `NOTES_REPO_URL`,
`GITHUB_TOKEN`, `PUBLIC_URL`, `GITHUB_OAUTH_CLIENT_ID`,
`GITHUB_OAUTH_CLIENT_SECRET`, `GITHUB_ALLOWED_LOGIN` (your GitHub username —
the only login allowed through), and `TUNNEL_TOKEN`. `chmod 600` it — it
holds all the secrets.

## Deploy / update

**Registry pull (default):** CI publishes `ghcr.io/<owner>/notes-mcp:latest`
(and a `sha-…` tag per commit) on every push to main. Set
`IMAGE=ghcr.io/<owner>/notes-mcp:latest` in your env file; then updating is
just:

```sh
sudo sh deploy.sh notes-mcp.env      # pulls the image and replaces both containers
```

Public repo → public image, no registry auth needed on the host. Pin a
`sha-…` tag instead of `latest` for rollbacks.

**Tarball (offline fallback):** build and `docker save` anywhere, copy it
over, and pass it as the second argument — the pull is skipped:

````sh
docker build -t notes-mcp:<ver> . && docker save notes-mcp:<ver> | gzip > notes-mcp-<ver>.tar.gz
sudo IMAGE=notes-mcp:<ver> sh deploy.sh notes-mcp.env notes-mcp-<ver>.tar.gz
``` The script is idempotent: it replaces both
containers; the `notes-repo` and `oauth-state` volumes persist, so updates
keep the repo clone, issued tokens, and the Claude connector registration.

Verify:

```sh
curl https://<host>/health                      # {"status":"ok"}
curl -i -X POST https://<host>/mcp | head -3    # 401 + WWW-Authenticate
sudo docker logs notes-mcp | tail               # structured JSON
````

## Network layout (and why)

- `notes-mcp` runs on Docker's **default bridge**.
- `cloudflared` runs with `--network container:notes-mcp` — it shares the
  server's network namespace and reaches it as `localhost:8000`.
- There is deliberately **no custom bridge network**: Synology's DSM
  firewall silently drops egress from CLI-created custom networks (DNS and
  all traffic time out) while the default bridge works.
- Corollary: if you ever recreate the `notes-mcp` container by hand,
  recreate `cloudflared` too — its namespace reference goes stale. The
  script always recreates both, in order.

## Synology (DSM) gotchas — all hit in practice

| Symptom                                                                  | Cause                                   | Fix                                                           |
| ------------------------------------------------------------------------ | --------------------------------------- | ------------------------------------------------------------- |
| `NanoCPUs can not be set`                                                | DSM kernel has no CFS scheduler         | don't use `--cpus` (script already doesn't)                   |
| `scp: subsystem request failed`                                          | DSM has no SFTP subsystem by default    | `scp -O`, or enable SFTP in File Services                     |
| `Could not resolve host: github.com` from containers on a custom network | DSM firewall drops custom-bridge egress | use the default-bridge + shared-netns layout (script default) |
| `docker: command not found` / permission denied                          | no docker group on DSM                  | `sudo`, binary at `/usr/local/bin/docker`                     |
| custom SSH port                                                          | DSM often runs SSH off 22               | `ssh -p <port>` but `scp -P <port>` (capital P)               |

## Troubleshooting

- **`curl /health` → 530 / error 1033**: tunnel has no connector →
  `sudo docker logs cloudflared` (token mangled? egress blocked?).
- **`/health` 200 but connector won't connect**: check
  `/.well-known/oauth-authorization-server` serves, then
  `sudo docker logs notes-mcp` during a Connect attempt — the OAuth steps log
  (`registered client`, `authorization granted`, or a named rejection).
- **GitHub login succeeds but 403**: the logged-in GitHub user isn't
  `GITHUB_ALLOWED_LOGIN` — the JSON error names the login it saw.
- **Writes fail with `merge_conflict`**: by design; resolve on desktop, the
  next write pulls fresh.
- **Container restart-loops**: startup validation failed — the last JSON log
  line names the missing/invalid variable or the failing git step.
- **Force-expire all tokens**: `sudo docker exec notes-mcp rm /data/signing_secret`
  then restart the container; reconnect the Claude app.
