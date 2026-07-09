# Deploy runbook: script deploys, continuous deploy, Synology

The standard self-host path is `docker compose up -d` — see
[Self-hosting in the main README](../README.md#self-hosting) for the full
walkthrough (GitHub OAuth app, PAT, tunnel, env file, connecting Claude).

This runbook covers the **script-based path** (`deploy.sh` — plain
`docker run`, no compose), which exists for hosts where compose networking
is a liability, chiefly Synology/DSM. It assumes the one-time setup from the
main README is done and you have a filled-in env file (`chmod 600`).

One difference from the compose path: cloudflared here shares the server's
network namespace, so the **tunnel ingress target is
`http://localhost:8000`**, not `http://notes-mcp:8000`.

## Deploy / update

**Registry pull (default):** CI publishes `ghcr.io/johnhringiv/notes-mcp:latest`
(and a `sha-…` tag per commit) on every push to main, plus a version tag
(`:0.2.0`) for every `v*` git tag; forks publish to their own GHCR. Set `IMAGE=` in your env file if the default in `.env.example`
isn't what you want; then updating is just:

```sh
sudo sh deploy.sh notes-mcp.env      # pulls the image and replaces both containers
```

Public repo → public image, no registry auth needed on the host. Pin a
`sha-…` tag instead of `latest` for rollbacks.

**Tarball (offline fallback):** build and `docker save` anywhere, copy it
over, and pass it as the second argument — the pull is skipped:

```sh
docker build -t notes-mcp:<ver> . && docker save notes-mcp:<ver> | gzip > notes-mcp-<ver>.tar.gz
sudo IMAGE=notes-mcp:<ver> sh deploy.sh notes-mcp.env notes-mcp-<ver>.tar.gz
```

The script is idempotent: it replaces both containers; the `notes-repo` and
`oauth-state` volumes persist, so updates keep the repo clone, issued
tokens, and the Claude connector registration.

Verify:

```sh
curl https://<host>/health                      # {"status":"ok","version":"…"}
curl -i -X POST https://<host>/mcp | head -3    # 401 + WWW-Authenticate
sudo docker logs notes-mcp | tail               # structured JSON
```

## Continuous deploy

The host can't accept inbound connections, so deploys are pull-based:

1. **Promote:** GitHub → Actions → **Deploy** → Run workflow. It retags a
   published image (default `latest`; pass a `sha-…` tag to roll back) to
   `:prod` on GHCR. Registry-side only — takes seconds.
2. **Poll:** the host runs `poll-deploy.sh` on a schedule. Unchanged tag →
   one manifest check, exit. Changed → runs `deploy.sh` and verifies
   `/health`, exiting non-zero if unhealthy.

Set `IMAGE=ghcr.io/<owner>/notes-mcp:prod` in the env file, then schedule
(on Synology: Control Panel → Task Scheduler → Create → Scheduled Task →
User-defined script, user **root**, repeat every 5 minutes; elsewhere: cron):

```sh
cd /path/to/deploy/dir && sh poll-deploy.sh notes-mcp.env >> poll-deploy.log 2>&1
```

On Synology, in the task's Settings tab, enable "Send run details by email …
only when the script terminates abnormally" to get notified of unhealthy
deploys. Press the Deploy button once to mint `:prod` before the first poll.
To ship every push to main instead, point `IMAGE` at `:latest` — same
poller, no button.

## Network layout (and why)

- `notes-mcp` runs on Docker's **default bridge**.
- `cloudflared` runs with `--network container:notes-mcp` — it shares the
  server's network namespace and reaches it as `localhost:8000`.
- There is deliberately **no custom bridge network**: Synology's DSM
  firewall silently drops egress from CLI-created custom networks (DNS and
  all traffic time out) while the default bridge works. (This is exactly why
  compose — which creates a custom network — is avoided on DSM.)
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

Applies to both the compose and script paths.

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
