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

docker compose run --rm --no-deps api job-hunt config validate --live

# Always ask Compose to build the selected services before starting them.
# BuildKit reuses cached layers for unchanged images, while this also handles
# newly added services when the repository was pulled before this script ran.
docker compose up -d --build --force-recreate --remove-orphans \
    api worker-fast worker-documents worker-notifications beat flower

docker compose ps
wait_for_http "API live" "http://127.0.0.1:8000/health/live"
wait_for_http "API ready" "http://127.0.0.1:8000/health/ready"
docker compose exec -T redis redis-cli ping
