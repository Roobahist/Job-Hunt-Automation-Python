# Job Hunt Automation

A Python 3.12, multi-tenant replacement for the Mahsa and Mojtaba n8n workflows. The application has one domain workflow and explicit provider/renderer boundaries; tenant profiles contain only bootstrap configuration, master CV data, templates, and schema-specific document rendering.

## Services

- FastAPI accepts authenticated Fillout and operator requests and returns a run ID immediately.
- Celery workers normalize, deduplicate, score, tailor, render, upload, persist, and notify.
- Celery Beat checks hourly for tenants whose configured discovery interval is due.
- Redis provides queues, run state, replay data, locks, and idempotency coordination.
- Baserow remains the business system of record and prompt/configuration source.

Documents are generated when `score >= qualification_threshold`. The `should_apply` value is retained as qualification metadata but does not gate document generation. Authenticated API/CLI submissions can deliberately pass `force=true`; Fillout and scheduled discovery cannot.

## Local setup

1. Install [uv](https://docs.astral.sh/uv/) and Docker.
2. Run `uv sync --extra dev` and copy `.env.example` to `.env`.
3. Import each `config/seeds/*.csv` into a Baserow Configuration table and put its ID in `config/users.toml`.
4. Fill credential environment variables. No credential belongs in TOML, CSV, source, or logs.
5. Validate files with `uv run job-hunt config validate`; add `--live` after credentials and Baserow are ready.
6. Start the stack with `docker compose up --build -d`.
7. Run all offline checks with `make check`.

OpenAPI documentation is available at `http://localhost:8000/docs`. See [Operations](docs/operations.md), [tenant onboarding](docs/tenant-onboarding.md), and [VPS deployment](docs/vps-deployment.md).

## Operator examples

```bash
curl -X POST http://localhost:8000/v1/tenants/mahsa/jobs \
  -H "Authorization: Bearer $JOB_HUNT_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entry_type":"linkedin","linkedin_job_id":123456}'

uv run job-hunt submit mahsa --input examples/job.json
uv run job-hunt status RUN_UUID
uv run job-hunt retry RUN_UUID
uv run job-hunt discover mojtaba
```

## External API contracts

The adapters follow the official [Apify Python client](https://docs.apify.com/api/client/python/reference/class/ActorClient), [Baserow database API](https://baserow.io/user-docs/database-api), [Cloudinary upload API](https://cloudinary.com/documentation/upload_images), [Telegram `sendMediaGroup`](https://core.telegram.org/bots/api#sendmediagroup), [Fillout webhooks](https://www.fillout.com/help/webhooks), and [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output?lang=python) documentation.

