#!/bin/sh
# Single-image deploy for notes-mcp + cloudflared (no compose needed).
#
# Usage:
#   1. Copy .env.example to .env next to this script and fill it in
#      (NOTES_REPO_URL, GITHUB_TOKEN, PUBLIC_URL, GITHUB_OAUTH_*, TUNNEL_TOKEN).
#   2. Optionally place a saved image tarball next to it (docker save output).
#   3. sh deploy.sh [env-file] [image-tarball]
#
# Network layout: notes-mcp runs on Docker's default bridge; cloudflared
# joins notes-mcp's network namespace and reaches it as localhost:8000, so
# the tunnel ingress must target http://localhost:8000. Custom bridge
# networks are avoided deliberately — Synology's DSM firewall silently
# drops their egress.
#
# Idempotent: re-running replaces both containers; volumes persist.
set -e

ENV_FILE="${1:-.env}"
IMAGE_TAR="${2:-}"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy .env.example and fill it in)"; exit 1; }

# Image ref: $IMAGE env > IMAGE= line in the env file > local default.
if [ -z "$IMAGE" ]; then
    IMAGE=$(grep -E '^IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
IMAGE="${IMAGE:-notes-mcp:0.1.0}"

# cloudflared needs TUNNEL_TOKEN as its own env; pull it out of the env file.
TUNNEL_TOKEN=$(grep -E '^TUNNEL_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
[ -n "$TUNNEL_TOKEN" ] || { echo "TUNNEL_TOKEN is not set in $ENV_FILE"; exit 1; }

if [ -n "$IMAGE_TAR" ]; then
    docker load -i "$IMAGE_TAR"
else
    docker pull "$IMAGE" || echo "pull failed; will use local image $IMAGE if present"
fi

docker rm -f notes-mcp cloudflared 2>/dev/null || true

docker run -d --name notes-mcp --restart unless-stopped \
  --memory 2g \
  `# no --cpus: DSM kernels lack the CFS scheduler; script timeouts bound CPU use` \
  -v notes-repo:/repo -v oauth-state:/data \
  --env-file "$ENV_FILE" \
  "$IMAGE"

sleep 3
docker run -d --name cloudflared --restart unless-stopped \
  --network container:notes-mcp \
  -e TUNNEL_TOKEN="$TUNNEL_TOKEN" \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run

sleep 5
docker ps --filter name=notes-mcp --filter name=cloudflared --format '{{.Names}}: {{.Status}}'
docker logs notes-mcp 2>&1 | tail -5
PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
echo "verify: curl ${PUBLIC_URL:-https://your-hostname}/health"
