#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

requested_level="auto"
dry_run=false

usage() {
    cat <<'USAGE'
Usage: bash scripts/deploy-vps.sh [--level auto|0|1|2|3] [--dry-run]

Levels:
  0  Pull only. Documentation/tests/example-config changes; no runtime action.
  1  Runtime refresh. No image build; recreate services and reload mounted config.
  2  Application rebuild. Rebuild the lightweight app image, not the TeX document image.
  3  Full deployment. Rebuild app + document images and run full validations.

Default: --level auto. Auto mode compares the current VPS commit with its upstream branch
and selects the lowest safe level from the changed paths.
USAGE
}

while (($#)); do
    case "$1" in
        --level)
            requested_level="${2:-}"
            shift 2
            ;;
        --dry-run)
            dry_run=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$requested_level" in
    auto|0|1|2|3) ;;
    *)
        printf 'Invalid deployment level: %s\n' "$requested_level" >&2
        exit 2
        ;;
esac

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

level_name() {
    case "$1" in
        0) printf 'pull-only' ;;
        1) printf 'runtime-refresh' ;;
        2) printf 'application-rebuild' ;;
        3) printf 'full' ;;
    esac
}

classify_path() {
    local path="$1"

    # Documentation, tests, examples, and seed/reference data do not affect the running checkout.
    case "$path" in
        README.md|docs/*|tests/*|.env.example|config/seeds/*)
            printf '0'
            return
            ;;
    esac

    # These paths are bind-mounted or external runtime configuration. No image build is required.
    case "$path" in
        config/users.toml|config/llm-providers.json|tenants/*|deploy/nginx/*)
            printf '1'
            return
            ;;
    esac

    # HTTP/operator-only code can use the lightweight application image without rebuilding TeX.
    case "$path" in
        src/job_hunt/api/*|src/job_hunt/cli.py|src/job_hunt/queueing.py|scripts/generate-litellm-config.py)
            printf '2'
            return
            ;;
    esac

    # Shared worker code, dependency/build files, Compose, and unknown runtime files take the safe path.
    printf '3'
}

needs_litellm_reload=false
needs_latex_check=false

old_head="$(git rev-parse HEAD)"
git fetch --prune origin
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || printf 'origin/main')"
new_head="$(git rev-parse "$upstream")"

mapfile -t changed_files < <(git diff --name-only "$old_head" "$new_head")

if [[ "$requested_level" == "auto" ]]; then
    deploy_level=0
    for path in "${changed_files[@]}"; do
        path_level="$(classify_path "$path")"
        if ((path_level > deploy_level)); then
            deploy_level="$path_level"
        fi
        case "$path" in
            config/llm-providers.json|scripts/generate-litellm-config.py|src/job_hunt/integrations/litellm_config.py)
                needs_litellm_reload=true
                ;;
        esac
        case "$path" in
            tenants/*/master_cv.json|tenants/*/templates/*|src/job_hunt/rendering/*)
                needs_latex_check=true
                ;;
        esac
    done
else
    deploy_level="$requested_level"
    # Explicit runtime-or-higher deployments are commonly used after VPS-local .env changes,
    # which Git cannot detect. Reload LiteLLM so provider/key changes are applied.
    if ((deploy_level >= 1)); then
        needs_litellm_reload=true
    fi
    if ((deploy_level >= 3)); then
        needs_latex_check=true
    fi
fi

printf 'Current commit: %s\n' "$old_head"
printf 'Upstream commit: %s\n' "$new_head"
printf 'Deployment level: %s (%s)\n' "$deploy_level" "$(level_name "$deploy_level")"
if ((${#changed_files[@]})); then
    printf 'Changed files:\n'
    printf '  %s\n' "${changed_files[@]}"
else
    printf 'Changed files: none\n'
fi

if "$dry_run"; then
    printf 'Dry run only; repository and containers were not changed.\n'
    exit 0
fi

# Fast-forward the already-fetched upstream commit. This refuses accidental merge commits.
git merge --ff-only "$upstream"

if ((deploy_level == 0)); then
    printf 'Level 0 complete: repository updated; no runtime services needed changes.\n'
    exit 0
fi

if ((deploy_level >= 2)); then
    if ((deploy_level >= 3)); then
        # Build both targets in one BuildKit invocation. The expensive TeX installation is a stable
        # cached layer and is only part of the document image.
        docker compose build api worker-documents
    else
        docker compose build api
    fi
fi

reload_litellm() {
    # Keep application config mounts read-only. The container emits the generated LiteLLM config,
    # while the host owns the atomic write into config/litellm.runtime.yaml.
    local runtime_tmp
    runtime_tmp="$(mktemp ./config/litellm.runtime.yaml.XXXXXX)"
    trap 'rm -f "$runtime_tmp"' RETURN
    docker compose run --rm -T --no-deps \
        -e LITELLM_CONFIG_STDOUT=true \
        api python scripts/generate-litellm-config.py >"$runtime_tmp"
    mv "$runtime_tmp" ./config/litellm.runtime.yaml
    trap - RETURN

    docker compose up -d redis
    docker compose up -d --force-recreate litellm
    wait_for_litellm

    # Verify every configured generation and repair group is exposed by the live gateway.
    docker compose run --rm --no-deps api job-hunt config validate --live
}

if "$needs_litellm_reload" || ((deploy_level >= 2)); then
    reload_litellm
fi

if "$needs_latex_check"; then
    docker compose run --rm --no-deps worker-documents python scripts/check-mahsa-latex.py
fi

if ((deploy_level == 1)); then
    # Config and tenant assets are mounted from the host, so recreating is enough.
    docker compose up -d --force-recreate --remove-orphans \
        api worker-fast worker-documents worker-notifications beat flower
elif ((deploy_level == 2)); then
    # Leave the heavy document worker untouched; this level is reserved for API/operator-only code.
    docker compose up -d --force-recreate --remove-orphans \
        api worker-fast worker-notifications beat flower
else
    docker compose up -d --force-recreate --remove-orphans \
        api worker-fast worker-documents worker-notifications beat flower
fi

docker compose ps
wait_for_http "API live" "http://127.0.0.1:8000/health/live"
wait_for_http "API ready" "http://127.0.0.1:8000/health/ready"
docker compose exec -T redis redis-cli ping
printf 'Deployment complete at level %s (%s).\n' "$deploy_level" "$(level_name "$deploy_level")"
