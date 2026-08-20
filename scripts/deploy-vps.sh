#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

wait_for_http() {
    local name="$1"
    local url="$2"
    local attempts="${3:-30}"

    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if response=$(curl --fail --silent --show-error "$url" 2>/dev/null); then
            printf '%s: %s\n' "$name" "$response"
            return 0
        fi
        sleep 1
    done

    printf '%s did not become ready after %s seconds\n' "$name" "$attempts" >&2
    docker compose logs --tail=100 api >&2 || true
    return 1
}

git pull --ff-only

docker compose build
docker compose run --rm api job-hunt config validate --live
docker compose up -d

docker compose ps
wait_for_http "API live" "http://127.0.0.1:8000/health/live"
wait_for_http "API ready" "http://127.0.0.1:8000/health/ready"
docker compose exec -T redis redis-cli ping
