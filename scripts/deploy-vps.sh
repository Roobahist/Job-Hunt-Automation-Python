#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

git pull --ff-only

docker compose build
docker compose run --rm api job-hunt config validate --live
docker compose up -d

docker compose ps
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
printf '\n'
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
printf '\n'
docker compose exec -T redis redis-cli ping
