# VPS deployment guide

This repository does not connect to or modify the VPS automatically. Deploy only after local and CI checks pass.

1. Install Docker Engine and Docker Compose and clone the repository.
2. Create `.env` from `.env.example`. Use independent long operator and Fillout secrets, plus one shared Telegram webhook secret, and restrict file permissions.
3. Update `config/users.toml` with the real Baserow Configuration table IDs. Keep provider credentials in `.env` or a VPS secret manager.
4. Install the repository-managed nginx site from `deploy/nginx/job-hunt-automation.conf` after the TLS certificate for `automation.mojtabakanani.me` exists. The config exposes only the Fillout webhook prefix and the exact shared Telegram webhook path. FastAPI and Flower remain bound to localhost and Redis remains Docker-internal.
5. Run `scripts/deploy-vps.sh` for normal deployments. It performs a fast-forward-only pull, builds the images, runs live configuration validation, starts the stack, and verifies API and Redis health.
6. Configure Fillout webhooks after readiness succeeds. Register the single shared Telegram bot webhook at `/webhooks/telegram` using `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` as Telegram's secret token. Tenant callback routing uses each Baserow configuration's `telegram_chat_id`.
7. Configure Docker log rotation. Back up Redis because it contains run replay data, discovery snapshots, provider state, and LLM checkpoints. Baserow remains the durable document/business store.
8. Watch Flower, structured logs, and optionally Langfuse during the first discovery and manual submissions.

The API and Beat images use the lightweight Docker target and do not include TeX. Only the worker target installs the LaTeX packages required for PDF rendering. This keeps non-rendering containers smaller while preserving one shared Python dependency layer.

Scale Celery workers independently when needed. `worker_prefetch_multiplier=1` should remain enabled because application jobs can contain long provider and document-rendering stages. Gemini RPM/TPM/RPD coordination is Redis-backed, so additional worker processes share the same proactive quota counters and cooldown state.

The VPS checkout should use a read-only deploy key. Repository changes are made in GitHub and pulled to the VPS; live `.env`, nginx installation, DNS, and certificate state are VPS-specific and should not be committed.
