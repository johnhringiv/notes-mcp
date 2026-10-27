#!/bin/sh
# Single-image deploy for notes-mcp + cloudflared (no compose needed).
#
# Usage:
#   1. Copy .env.example to .env next to this script and fill it in
#      (NOTES_REPO_URL, GITHUB_TOKEN, PUBLIC_URL, GITHUB_OAUTH_*, TUNNEL_TOKEN).
#   2. Optionally place a saved image tarball next to it (docker save output).
#   3. sh deploy.sh [env-file] [image-tarball]
#
#   ONLY=tunnel sh deploy.sh [env-file]
#      Replace just the cloudflared container (pulling its image first),
#      leaving notes-mcp untouched. poll-deploy.sh uses this when only the
#      tunnel image changed. Requires notes-mcp to be running.
#
# Network layout: notes-mcp runs on Docker's default bridge; cloudflared
# joins notes-mcp's network namespace and reaches it as localhost:8000, so
# the tunnel ingress must target http://localhost:8000. Custom bridge
# networks are avoided deliberately — Synology's DSM firewall silently
# drops their egress.
#
# Idempotent: re-running replaces both containers; volumes persist.
#
# The tunnel image is CLOUDFLARED_IMAGE (env > env file > default
# cloudflare/cloudflared:latest). Pin a version tag there to opt out of
# tunnel updates.
set -e

ENV_FILE="${1:-.env}"
IMAGE_TAR="${2:-}"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy .env.example and fill it in)"; exit 1; }

# Image ref: $IMAGE env > IMAGE= line in the env file > public registry image.
if [ -z "$IMAGE" ]; then
    IMAGE=$(grep -E '^IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
IMAGE="${IMAGE:-ghcr.io/johnhringiv/notes-mcp:latest}"

if [ -z "$CLOUDFLARED_IMAGE" ]; then
    CLOUDFLARED_IMAGE=$(grep -E '^CLOUDFLARED_IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"

# cloudflared needs TUNNEL_TOKEN as its own env; pull it out of the env file.
TUNNEL_TOKEN=$(grep -E '^TUNNEL_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
[ -n "$TUNNEL_TOKEN" ] || { echo "TUNNEL_TOKEN is not set in $ENV_FILE"; exit 1; }

# `docker run` only pulls when the image is missing locally, so without an
# explicit pull a :latest tag would stay at whatever version was first seen.
pull_tunnel() {
    docker pull "$CLOUDFLARED_IMAGE" || echo "pull failed; will use local image $CLOUDFLARED_IMAGE if present"
}

start_tunnel() {
    docker run -d --name cloudflared --restart unless-stopped \
      --network container:notes-mcp \
      -e TUNNEL_TOKEN="$TUNNEL_TOKEN" \
      "$CLOUDFLARED_IMAGE" tunnel --no-autoupdate run
}

show_status() {
    docker ps --filter name=notes-mcp --filter name=cloudflared --format '{{.Names}}: {{.Status}}'
}

if [ "$ONLY" = "tunnel" ]; then
    [ "$(docker inspect notes-mcp --format '{{.State.Running}}' 2>/dev/null)" = "true" ] \
        || { echo "notes-mcp is not running; run a full deploy instead"; exit 1; }
    pull_tunnel
    docker rm -f cloudflared 2>/dev/null || true
    start_tunnel
    sleep 5
    show_status
    docker logs cloudflared 2>&1 | tail -5
    exit 0
elif [ -n "$ONLY" ]; then
    echo "unknown ONLY=$ONLY (supported: tunnel)"
    exit 1
fi

if [ -n "$IMAGE_TAR" ]; then
    docker load -i "$IMAGE_TAR"
else
    docker pull "$IMAGE" || echo "pull failed; will use local image $IMAGE if present"
fi
pull_tunnel

docker rm -f notes-mcp cloudflared 2>/dev/null || true

docker run -d --name notes-mcp --restart unless-stopped \
  --memory 2g \
  `# no --cpus: DSM kernels lack the CFS scheduler; script timeouts bound CPU use` \
  -v notes-repo:/repo -v oauth-state:/data \
  --env-file "$ENV_FILE" \
  "$IMAGE"

sleep 3
start_tunnel

sleep 5
show_status
docker logs notes-mcp 2>&1 | tail -5
PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
echo "verify: curl ${PUBLIC_URL:-https://your-hostname}/health"
