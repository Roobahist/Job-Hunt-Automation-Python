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

before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)

needs_build=false
if [[ "$before" != "$after" ]]; then
    if git diff --name-only "$before" "$after" -- \
        Dockerfile pyproject.toml uv.lock | grep -q .; then
        needs_build=true
    fi
fi

if [[ "$needs_build" == true ]]; then
    printf 'Dependency/container definition changed; rebuilding images.\n'
    docker compose build
else
    printf 'Application-only change; reusing existing images.\n'
fi

docker compose run --rm --no-deps api job-hunt config validate --live

docker compose up -d --no-build --force-recreate api worker-fast worker-documents beat flower

docker compose ps
wait_for_http "API live" "http://127.0.0.1:8000/health/live"
wait_for_http "API ready" "http://127.0.0.1:8000/health/ready"
docker compose exec -T redis redis-cli ping
