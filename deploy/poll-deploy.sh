#!/bin/sh
# Poll the deploy image tag and redeploy when its digest changes.
#
# Runs from a scheduler on the host (e.g. Synology DSM Task Scheduler, every
# 5 minutes, as root). An unchanged tag costs one manifest check (~KBs);
# layers only download when the Deploy workflow has promoted a new image.
#
#   sh poll-deploy.sh [env-file]
#
# Exit codes: 0 = no-op or successful deploy; 1 = deploy ran but health
# check failed (configure the scheduler to notify on abnormal termination).
set -e

ENV_FILE="${1:-.env}"
DIR=$(dirname "$0")

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 1; }

if [ -z "$IMAGE" ]; then
    IMAGE=$(grep -E '^IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
fi
[ -n "$IMAGE" ] || { echo "IMAGE not set in $ENV_FILE"; exit 1; }

# Before the first promotion the tag may not exist; that is not an error.
if ! docker pull -q "$IMAGE" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) pull failed for $IMAGE (tag not published yet?); skipping"
    exit 0
fi

latest=$(docker image inspect "$IMAGE" --format '{{.Id}}')
running=$(docker inspect notes-mcp --format '{{.Image}}' 2>/dev/null || echo "none")

[ "$latest" = "$running" ] && exit 0

echo "$(date -u +%FT%TZ) new image for $IMAGE (running=$running -> $latest); deploying"
sh "$DIR/deploy.sh" "$ENV_FILE"

PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$ENV_FILE" | tail -1 | cut -d= -f2-)
if [ -n "$PUBLIC_URL" ]; then
    i=0
    while [ $i -lt 12 ]; do
        if curl -sf -m 5 "$PUBLIC_URL/health" >/dev/null 2>&1; then
            echo "$(date -u +%FT%TZ) deploy healthy"
            docker image prune -f >/dev/null
            exit 0
        fi
        i=$((i + 1))
        sleep 5
    done
    echo "$(date -u +%FT%TZ) DEPLOY UNHEALTHY: $PUBLIC_URL/health not responding"
    exit 1
fi
echo "$(date -u +%FT%TZ) deployed (no PUBLIC_URL; health check skipped)"
