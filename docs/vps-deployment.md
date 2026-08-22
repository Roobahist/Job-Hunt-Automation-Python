# VPS deployment guide

Production changes should be made in GitHub and deployed through the repository script. VPS-local `.env`, nginx installation, DNS, and certificate state remain outside Git.

## Initial setup

1. Install Docker Engine and Docker Compose and clone the repository.
2. Create `.env` from `.env.example`, populate application/provider/tenant secrets, and restrict permissions.
3. Update `config/users.toml` with real Baserow Configuration table IDs.
4. Install `deploy/nginx/job-hunt-automation.conf` after TLS is ready. FastAPI/Flower stay localhost-bound and Redis/LiteLLM stay Docker-internal.
5. Register Fillout and the shared Telegram webhook only after readiness succeeds.
6. Configure Docker log rotation and back up Redis because it contains replay data, snapshots, cooldown state, and LLM checkpoints. Baserow remains the durable business/document store.

## Normal deployment

```bash
cd /opt/job-hunt-automation
bash scripts/deploy-vps.sh
```

Auto mode:

1. fetches the configured upstream branch
2. compares the currently deployed commit with upstream
3. selects the lowest safe deployment level
4. fast-forwards the checkout
5. performs only required build/reload work
6. validates runtime health

Preview without changing checkout/containers:

```bash
bash scripts/deploy-vps.sh --dry-run
```

### Important auto-detection rule

Run auto deployment before manually pulling/resetting the checkout. If you first run `git reset --hard origin/main`, the old deployed commit is no longer available to the script as its changed-path baseline. In that recovery pattern, use an explicit level.

Example recovery deployment:

```bash
cd /opt/job-hunt-automation
git fetch origin
git reset --hard origin/main
bash scripts/deploy-vps.sh --level 3
```

## Deployment levels

| Level | Name | Use when | Main actions |
| --- | --- | --- | --- |
| `0` | pull-only | docs/tests/example/non-runtime tracked files | fast-forward checkout only |
| `1` | runtime-refresh | mounted config/tenant assets or VPS-local `.env` | regenerate/recreate required runtime services, no image build |
| `2` | application-rebuild | narrow API/operator Python | rebuild lightweight application image |
| `3` | full | shared Python, workers, integrations, rendering, dependencies, Docker/Compose, deployment scripts | rebuild app + document images, run full validation, recreate services |

Explicit examples:

```bash
bash scripts/deploy-vps.sh --level 1
bash scripts/deploy-vps.sh --level 2
bash scripts/deploy-vps.sh --level 3
```

Use the highest required level when a change spans categories.

## Image layout

The lightweight application image is reused by:

- API
- `worker-fast`
- `worker-notifications`
- Beat
- Flower

Only `worker-documents` uses the TeX-enabled document image. There is no separate Beat image target.

The TeX installation is isolated in a stable Docker layer so source-only Level 3 builds can reuse it when Docker/TeX dependencies did not change.

Mounted `config/` and tenant assets can use Level 1. LiteLLM runtime config is regenerated/reloaded when the selected deployment path requires it.

## Provider/config changes

A provider key change in VPS-local `.env` is invisible to Git, so use Level 1 explicitly:

```bash
bash scripts/deploy-vps.sh --level 1
```

A change only to `config/llm-providers.json` also needs Level 1.

A change to `integrations/llm_routing.py`, workers, shared application/domain code, or provider-config generation Python requires Level 3.

## Verification

The deployment script checks Compose state, API liveness/readiness, Redis, and required LiteLLM groups. Full deployments also run the heavier validations required by shared/document changes.

Repository tests on VPS:

```bash
bash scripts/test-vps.sh
```

Useful runtime checks:

```bash
docker compose ps
docker compose logs --since 10m litellm
docker compose logs --since 10m worker-fast
docker compose logs --since 10m worker-documents
```

## Operational notes

Keep `worker_prefetch_multiplier=1` because job duration varies significantly.

The current LiteLLM Compose image uses `ghcr.io/berriai/litellm:main-stable`. Pin a tested version/digest in a dedicated deployment change before treating the routing layer as fully reproducible.
