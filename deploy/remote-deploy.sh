#!/usr/bin/env bash
# Runs ON the EC2 instance; the GitHub Actions workflow pipes it over SSH.
# Starts the new image, waits for it to be healthy and rolls back to the
# previous container if it is not.
#
# Required environment: IMAGE (full image reference), REGISTRY, AWS_REGION
set -euo pipefail
: "${IMAGE:?IMAGE is required}" "${REGISTRY:?REGISTRY is required}" "${AWS_REGION:?AWS_REGION is required}"

NAME=chatbot

echo "==> Pulling $IMAGE"
sudo docker pull "$IMAGE"
sudo docker logout "$REGISTRY" >/dev/null 2>&1 || true   # don't leave the ECR token on disk

PREV_ID=$(sudo docker inspect --format '{{.Image}}' "$NAME" 2>/dev/null || true)
echo "==> Currently running image: ${PREV_ID:-none}"

start() {
  local image="$1" mount=()
  sudo docker rm -f "$NAME" >/dev/null 2>&1 || true
  # Use the AWS config on the box if there is one; with an IAM role there is nothing to mount.
  if [ -d "$HOME/aws" ]; then mount=(-v "$HOME/aws:/home/appuser/.aws:ro"); fi
  sudo docker run -d --name "$NAME" --restart unless-stopped \
    -p 8000:8000 -e AWS_REGION="$AWS_REGION" \
    "${mount[@]}" -v chat-data:/data \
    "$image" >/dev/null
}

wait_healthy() {
  local status
  for _ in $(seq 1 40); do
    status=$(sudo docker inspect --format '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo none)
    if [ "$status" = healthy ] && curl -fsS http://127.0.0.1:8000/health >/dev/null; then
      return 0
    fi
    sleep 3
  done
  return 1
}

echo "==> Starting new container"
start "$IMAGE"

if wait_healthy; then
  echo "==> New version is healthy"
  if curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
    echo "==> nginx proxy on port 80 is answering"
  else
    echo "note: nothing answered on port 80 (nginx proxy); port 8000 is fine"
  fi
  sudo docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true
  sudo docker ps --filter "name=$NAME" --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}'
  exit 0
fi

echo "::error::The new version did not become healthy"
sudo docker logs --tail 60 "$NAME" || true

if [ -n "$PREV_ID" ]; then
  echo "==> Rolling back to $PREV_ID"
  start "$PREV_ID"
  if wait_healthy; then echo "==> Rollback succeeded; the previous version is running"; else echo "::error::Rollback is also unhealthy"; fi
else
  echo "No previous container to roll back to."
fi
exit 1
