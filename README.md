# Job Hunt Automation

A Python 3.12 multi-tenant replacement for the Mahsa and Mojtaba n8n workflows. Both tenants share the same API, worker, provider, schema-validation, persistence, and document-generation infrastructure. Tenant-specific code is limited to CV assembly and rendering differences.

## Architecture

- FastAPI accepts Fillout, operator, and optional Telegram callback requests.
- Celery workers normalize jobs, qualify them, tailor application content, render PDFs, and persist results.
- Celery Beat dispatches scheduled discoveries.
- Redis provides Celery transport, run state, replay data, locks, discovery snapshots, LLM checkpoints, provider cooldowns, and proactive Gemini quota counters.
- Baserow is the business system of record, prompt/configuration source, and final document store.
- LangChain `ChatGoogleGenerativeAI` handles Gemini structured-output calls.
- Flower provides Celery task visibility on port 5555.
- Langfuse tracing is optional and enabled by environment variables only.

Documents are generated when `score >= qualification_threshold`. `should_apply` remains stored as qualification metadata but does not gate document generation. Operator API and CLI submissions can use `force=true`; Fillout and scheduled discovery cannot.

## Baserow prompt contract

Every active prompt is read from Baserow using these columns:

```text
Prompt Key
Version
Prompt Template
Output Structure
Temperature
Status
Enabled
```

Only rows with `Status = Active` and `Enabled = true` are used. The Baserow `Output Structure` is passed to Gemini as JSON Schema and independently validated afterward. If validation fails, a separate repair-model tier receives the malformed output, validation error, and exact schema. The repaired result must pass the same schema.

Successful structured results are checkpointed in Redis using a digest of the rendered prompt, prompt key/version, and JSON Schema. If a later stage fails, retrying the job reuses already completed LLM stages rather than spending quota regenerating them.

## Shared provider capacity

All tenants share application-wide pools. Each configured key/token is expected to belong to a separate free-tier account:

```bash
JOB_HUNT_APIFY_TOKENS=token1,token2,token3
JOB_HUNT_GEMINI_API_KEYS=key1,key2,key3
```

Provider cooldown state is stored in Redis, so all Celery worker processes see the same exhausted accounts. Retry metadata is used when available instead of applying one fixed cooldown to every failure.

Gemini primary generation is model-first and account-second. By default the workflow exhausts every account on the strongest model before moving down the list:

```text
gemini-3.6-flash
gemini-3.5-flash
gemini-3-flash-preview
gemini-2.5-flash
gemini-3.5-flash-lite
gemini-3.1-flash-lite
gemini-2.5-flash-lite
```

Structure repair starts directly with the higher-throughput Lite tier:

```text
gemini-3.5-flash-lite
gemini-3.1-flash-lite
gemini-2.5-flash-lite
```

`JOB_HUNT_GEMINI_LIMITS_JSON` can override the proactive RPM/TPM/RPD budgets. Redis counters reduce avoidable 429 requests, while provider responses remain the final source of truth. `job-hunt config validate --live` also checks that configured model IDs are exposed by the Gemini account.

## Discovery snapshots and parallel tailoring

A scheduled discovery loads the tenant configuration and active prompts once, stores that snapshot in Redis, and passes the same snapshot ID to every job produced by that discovery. Jobs in one batch therefore use the same prompt versions while avoiding repeated Baserow configuration reads.

Independent tailoring branches run concurrently within the configured `JOB_HUNT_LLM_PARALLELISM` limit. Mojtaba runs project tailoring, work-experience tailoring, and skills tailoring in parallel before summary generation. Mahsa runs work-experience tailoring, skills tailoring, and the references decision in parallel. Shared Redis quota counters coordinate those calls across workers.

## Persistence and notifications

Generated CV and cover-letter PDFs upload directly to Baserow. JSON and TeX sources remain in the local ZIP bundle, eliminating the previous Cloudinary transport hop.

Reprocessing never clears an existing working CV or cover letter before a replacement succeeds. After new documents are persisted, the Baserow row moves to `To Apply` when that status is configured.

Telegram delivery is a separate Celery task. One application-wide bot serves all tenants, while each tenant keeps its own `telegram_chat_id` in Baserow. Callback actions arrive at the single `/webhooks/telegram` endpoint and are routed to the matching tenant by the incoming chat ID. A Telegram outage cannot turn completed document generation into a failed application run or trigger another set of Gemini calls.

## Observability

Flower is exposed at `http://localhost:5555` by Docker Compose. Set `FLOWER_BASIC_AUTH=user:password` before exposing it beyond localhost.

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` to enable Langfuse tracing. Gemini logs and traces include the prompt key/version, selected model, anonymized account identifier, latency, repair usage, and provider usage metadata when available.

## Local setup

1. Install uv and Docker.
2. Run `uv sync --extra dev` and copy `.env.example` to `.env`.
3. Import the tenant configuration seed into a Baserow Configuration table and put its ID in `config/users.toml`.
4. Import the matching prompt seed into the tenant Prompts table.
5. Fill the shared Apify/Gemini pools, the shared Telegram bot token/webhook secret, and each tenant's Baserow and Fillout secrets.
6. Run `uv run job-hunt config validate`; use `--live` after provider credentials are configured.
7. Start the stack with `docker compose up --build -d`.
8. Run `uv run ruff check .`, `uv run mypy`, and `uv run pytest` locally. GitHub Actions runs the same quality checks automatically.

FastAPI documentation is available at `http://localhost:8000/docs`.

For the production VPS, use the tracked nginx configuration in `deploy/nginx/job-hunt-automation.conf` and `bash scripts/deploy-vps.sh` after the VPS-specific `.env`, DNS, and TLS certificate are configured.

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

See `docs/operations.md`, `docs/tenant-onboarding.md`, and `docs/vps-deployment.md` for deployment and troubleshooting details.
