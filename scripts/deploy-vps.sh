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
    docker compose logs --tail=100 api litellm >&2 || true
    return 1
}

wait_for_litellm() {
    local attempts="${1:-60}"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        if docker compose exec -T litellm python -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness', timeout=2)" \
            >/dev/null 2>&1; then
            printf 'LiteLLM live\n'
            return 0
        fi
        sleep 1
    done

    printf 'LiteLLM did not become ready after %s seconds\n' "$attempts" >&2
    docker compose logs --tail=150 litellm >&2 || true
    return 1
}

git pull --ff-only

docker compose build api

# Expand numbered provider keys and the current Groq model catalog into capability pools.
# The generated runtime file is intentionally ignored by Git.
docker compose run --rm --no-deps api python scripts/generate-litellm-config.py

docker compose build worker-fast

docker compose run --rm --no-deps worker-fast python scripts/check-mahsa-latex.py

# Recreate the gateway so every deployment reloads the newly generated provider/model pools.
docker compose up -d redis
docker compose up -d --force-recreate litellm
wait_for_litellm

# This queries LiteLLM /v1/models and verifies every configured generation and repair group exists.
docker compose run --rm --no-deps api job-hunt config validate --live

docker compose up -d --force-recreate --remove-orphans \
    api worker-fast worker-documents worker-notifications beat flower

docker compose ps
wait_for_http "API live" "http://127.0.0.1:8000/health/live"
wait_for_http "API ready" "http://127.0.0.1:8000/health/ready"
docker compose exec -T redis redis-cli ping
