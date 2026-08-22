# VPS deployment guide

This repository does not connect to or modify the VPS automatically. Deploy only after local and CI checks pass.

1. Install Docker Engine and Docker Compose and clone the repository.
2. Create `.env` from `.env.example`. Use independent long operator and Fillout secrets, plus one shared Telegram webhook secret, and restrict file permissions.
3. Update `config/users.toml` with the real Baserow Configuration table IDs. Keep provider credentials in `.env` or a VPS secret manager.
4. Install the repository-managed nginx site from `deploy/nginx/job-hunt-automation.conf` after the TLS certificate for `automation.mojtabakanani.me` exists. The config exposes only the Fillout webhook prefix and the exact shared Telegram webhook path. FastAPI and Flower remain bound to localhost and Redis remains Docker-internal.
5. Use `bash scripts/deploy-vps.sh` for normal deployments. Auto mode fetches the upstream branch, compares it with the currently deployed commit, selects the lowest safe deployment level, fast-forwards the checkout, performs only the necessary build/reload work, and verifies runtime health.
6. Configure Fillout webhooks after readiness succeeds. Register the single shared Telegram bot webhook at `/webhooks/telegram` using `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` as Telegram's secret token. Tenant callback routing uses each Baserow configuration's `telegram_chat_id`.
7. Configure Docker log rotation. Back up Redis because it contains run replay data, discovery snapshots, provider state, and LLM checkpoints. Baserow remains the durable document/business store.
8. Watch Flower, structured logs, and optionally Langfuse during the first discovery and manual submissions.

## Deployment levels

The deployment script supports four explicit levels plus automatic selection:

| Level | Name | Use when | Main actions |
| --- | --- | --- | --- |
| `0` | pull-only | README/docs/tests/example config or other non-runtime tracked files | fast-forward Git checkout only |
| `1` | runtime-refresh | mounted tenant/config files or VPS-local `.env` changes | no image build; reload required runtime config and recreate services |
| `2` | application-rebuild | API/operator-only Python changes | rebuild lightweight application image; leave TeX document image untouched |
| `3` | full | shared worker code, rendering, dependencies, Docker/Compose, deployment scripts, or uncertain runtime changes | rebuild application and document images, run full validations, recreate all services |

Normal usage:

```bash
bash scripts/deploy-vps.sh
```

Preview what auto mode would do without changing the checkout or containers:

```bash
bash scripts/deploy-vps.sh --dry-run
```

Force a level when the reason is not visible to Git, especially after editing the VPS-local `.env`:

```bash
bash scripts/deploy-vps.sh --level 1
```

A full deployment remains available as the safe escape hatch:

```bash
bash scripts/deploy-vps.sh --level 3
```

Auto classification intentionally errs toward the safer level for shared Python/runtime files. API-only code can use Level 2. Most other `src/` changes use Level 3 because the document worker imports shared application, workflow, integration, and rendering code.

## Why deployment is split this way

The API, `worker-fast`, `worker-notifications`, Beat, and Flower all use the same lightweight application image. Only `worker-documents` uses the TeX-enabled document image. This prevents unrelated API or operator changes from rebuilding or redeploying the large LaTeX image.

The TeX installation is also isolated in a stable Docker layer. When the document image really must be rebuilt, ordinary source changes can reuse the cached TeX layer unless the TeX package list or an earlier Docker layer changed.

Level 3 builds the application and document targets in one Docker Compose build invocation so BuildKit can share/cache common layers efficiently.

Mounted files under `config/` and `tenants/` do not require image rebuilds. Level 1 recreates the relevant services so runtime configuration is refreshed without paying the Docker build cost.

LiteLLM runtime configuration is regenerated only when required by the selected/forced deployment path. The script recreates LiteLLM and validates its configured capability groups before application services are considered healthy.

LaTeX validation runs when tenant document assets/rendering require it or during a forced full deployment; documentation and unrelated application changes do not pay that cost.

## First deployment after deployment-system changes

When the deployment script, Dockerfile, or Compose topology itself changes, use Level 3 once so the VPS adopts the new image/service layout completely. After that migration deployment, normal deployments should return to auto mode.

## Operational notes

Scale Celery workers independently when needed. `worker_prefetch_multiplier=1` should remain enabled because application jobs can contain long provider and document-rendering stages.

The VPS checkout should use a read-only deploy key. Repository changes are made in GitHub and pulled to the VPS; live `.env`, nginx installation, DNS, and certificate state are VPS-specific and should not be committed.
