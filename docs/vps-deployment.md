# VPS deployment guide

This repository does not connect to or modify the VPS automatically. Deploy only after local and CI checks pass.

1. Install Docker Engine and Docker Compose and clone the repository.
2. Create `.env` from `.env.example`. Use independent long operator and Fillout secrets, plus one shared Telegram webhook secret, and restrict file permissions.
3. Update `config/users.toml` with the real Baserow Configuration table IDs. Keep provider credentials in `.env` or a VPS secret manager.
4. Run `docker compose build`, then `docker compose run --rm api job-hunt config validate --live`.
5. Start the stack with `docker compose up -d` and confirm `/health/ready` succeeds.
6. Put TLS/reverse proxying in front of FastAPI port 8000. Docker Compose binds FastAPI and Flower to localhost. Redis remains Docker-internal.
7. Configure Fillout webhooks after readiness succeeds. Register the single shared Telegram bot webhook at `/webhooks/telegram` using `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` as Telegram's secret token. Tenant callback routing uses each Baserow configuration's `telegram_chat_id`.
8. Configure Docker log rotation. Back up Redis because it now contains run replay data, discovery snapshots, provider state, and LLM checkpoints. Baserow remains the durable document/business store.
9. Watch Flower, structured logs, and optionally Langfuse during the first discovery and manual submissions.

The API and Beat images use the lightweight Docker target and do not include TeX. Only the worker target installs the LaTeX packages required for PDF rendering. This keeps non-rendering containers smaller while preserving one shared Python dependency layer.

Scale Celery workers independently when needed. `worker_prefetch_multiplier=1` should remain enabled because application jobs can contain long provider and document-rendering stages. Gemini RPM/TPM/RPD coordination is Redis-backed, so additional worker processes share the same proactive quota counters and cooldown state.

Upgrade with a CI-tested commit and `docker compose up -d --build`. Roll back to the previous image or Git commit and restart the services. Baserow data and existing PDFs are not deleted by reprocessing failures.
