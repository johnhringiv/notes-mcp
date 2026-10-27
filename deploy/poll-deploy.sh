#!/bin/sh
# Poll the deploy image tag and redeploy when its digest changes — and
# self-heal: an unchanged tag with the containers not running (typical after
# a host reboot, where cloudflared can start before notes-mcp and fail to
# join its network namespace) also triggers a redeploy, which recreates both
# containers in the correct order.
#
# The tunnel image (CLOUDFLARED_IMAGE, default cloudflare/cloudflared:latest)
# is followed too: when only it changed, just the cloudflared container is
# replaced (ONLY=tunnel deploy.sh) and notes-mcp keeps running.
#
# Runs from a scheduler on the host (e.g. Synology DSM Task Scheduler, every
# 5 minutes, as root). An unchanged healthy tag costs one manifest check and
# one /health probe; layers only download when the Deploy workflow has
# promoted a new image.
#
#   sh poll-deploy.sh [env-file]
#
# Exit codes: 0 = no-op or successful deploy; 1 = unhealthy (a deploy ran
# but /health never came up, or the containers are running yet the edge is
# unreachable — tunnel/DNS trouble a redeploy wouldn't fix). Configure the
# scheduler to notify on abnormal termination.
set -e

ENV_FILE="${1:-.env}"
DIR=$(dirname "$0")

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 1; }

if [ -z "$IMAGE" ]; then
    IMAGE=$(grep -E '^IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
[ -n "$IMAGE" ] || { echo "IMAGE not set in $ENV_FILE"; exit 1; }

if [ -z "$CLOUDFLARED_IMAGE" ]; then
    CLOUDFLARED_IMAGE=$(grep -E '^CLOUDFLARED_IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
CLOUDFLARED_IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"
export CLOUDFLARED_IMAGE

PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)

container_running() {
    [ "$(docker inspect "$1" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

# Three attempts, 5s apart — one blip shouldn't page or churn containers.
edge_healthy() {
    [ -n "$PUBLIC_URL" ] || return 0
    i=0
    while [ $i -lt 3 ]; do
        curl -sf -m 5 "$PUBLIC_URL/health" >/dev/null 2>&1 && return 0
        i=$((i + 1))
        sleep 5
    done
    return 1
}

# After a (re)deploy: wait up to a minute for the edge to answer.
wait_healthy() {
    if [ -z "$PUBLIC_URL" ]; then
        echo "$(date -u +%FT%TZ) $1 (no PUBLIC_URL; health check skipped)"
        exit 0
    fi
    i=0
    while [ $i -lt 12 ]; do
        if curl -sf -m 5 "$PUBLIC_URL/health" >/dev/null 2>&1; then
            echo "$(date -u +%FT%TZ) $1 healthy"
            docker image prune -f >/dev/null
            exit 0
        fi
        i=$((i + 1))
        sleep 5
    done
    echo "$(date -u +%FT%TZ) $1 UNHEALTHY: $PUBLIC_URL/health not responding"
    exit 1
}

# Before the first promotion the tag may not exist; that is not an error.
if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) pull failed for $IMAGE (tag not published yet?); skipping"
    exit 0
fi

latest=$(docker image inspect "$IMAGE" --format '{{.Id}}')
running=$(docker inspect notes-mcp --format '{{.Image}}' 2>/dev/null || echo "none")

# A failed tunnel pull is not fatal: the running tunnel keeps working.
docker pull -q "$CLOUDFLARED_IMAGE" >/dev/null 2>&1 \
    || echo "$(date -u +%FT%TZ) pull failed for $CLOUDFLARED_IMAGE; keeping current tunnel"
tunnel_latest=$(docker image inspect "$CLOUDFLARED_IMAGE" --format '{{.Id}}' 2>/dev/null || echo "none")
tunnel_running=$(docker inspect cloudflared --format '{{.Image}}' 2>/dev/null || echo "none")

if [ "$latest" = "$running" ]; then
    if container_running notes-mcp && container_running cloudflared; then
        if [ "$tunnel_latest" != "none" ] && [ "$tunnel_latest" != "$tunnel_running" ]; then
            echo "$(date -u +%FT%TZ) new image for $CLOUDFLARED_IMAGE (running=$tunnel_running -> $tunnel_latest); replacing tunnel only"
            ONLY=tunnel sh "$DIR/deploy.sh" "$ENV_FILE"
            wait_healthy "tunnel update"
        fi
        if edge_healthy; then
            exit 0
        fi
        echo "$(date -u +%FT%TZ) UNHEALTHY: containers running but $PUBLIC_URL/health unreachable (tunnel/DNS?); not redeploying"
        exit 1
    fi
    echo "$(date -u +%FT%TZ) containers not running on unchanged image (host rebooted?); redeploying"
else
    echo "$(date -u +%FT%TZ) new image for $IMAGE (running=$running -> $latest); deploying"
fi

sh "$DIR/deploy.sh" "$ENV_FILE"
wait_healthy "deploy"
