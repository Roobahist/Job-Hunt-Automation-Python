# Job Hunt Automation

A Python 3.12, multi-tenant replacement for the Mahsa and Mojtaba n8n workflows. The application has one domain workflow and explicit provider/renderer boundaries; tenant profiles contain only bootstrap configuration, master CV data, templates, and schema-specific document rendering.

## Services

- FastAPI accepts authenticated Fillout and operator requests and returns a run ID immediately.
- Celery workers normalize, deduplicate, score, tailor, render, upload, persist, and notify.
- Celery Beat checks hourly for tenants whose configured discovery interval is due.
- Redis provides queues, run state, replay data, locks, and idempotency coordination.
- Baserow remains the business system of record and prompt/configuration source.
- LangChain `ChatGoogleGenerativeAI` handles Gemini calls. Each active Baserow prompt supplies its own prompt template, JSON Output Structure, temperature, and version.
- Apify and Gemini credentials are application-wide shared capacity pools. They are not assigned to individual tenants.

Documents are generated when `score >= qualification_threshold`. The `should_apply` value is retained as qualification metadata but does not gate document generation. Authenticated API/CLI submissions can deliberately pass `force=true`; Fillout and scheduled discovery cannot.

Prompt responses are generated with Gemini native JSON Schema structured output, then validated again against the exact Baserow `Output Structure`. If validation fails, a separate repair-model tier fixes the malformed response and the repaired result is validated again before it is accepted.

## Shared provider capacity

Set all free-tier provider credentials once in `.env`:

```bash
JOB_HUNT_APIFY_TOKENS=token1,token2,token3
JOB_HUNT_GEMINI_API_KEYS=key1,key2,key3
```

Apify tries the shared tokens in order. When an account reports quota, usage-limit, rate-limit, payment/capacity, or authentication exhaustion, that token is placed on cooldown and the same request moves to the next shared token.

Gemini uses two ordered model tiers. Main content generation defaults to:

```text
gemini-3.6-flash
gemini-3.5-flash
gemini-3-flash-preview
gemini-2.5-flash
```

For each content model, every configured API key is tried before the workflow moves to the next model. This keeps all available capacity on the strongest model before degrading to a weaker model. The list is controlled by `JOB_HUNT_GEMINI_CONTENT_MODELS`.

Structured-output repair uses a separate high-throughput tier so main-model quota is preserved:

```text
gemini-3.5-flash-lite
gemini-3.1-flash-lite
gemini-2.5-flash-lite
```

The repair list is controlled by `JOB_HUNT_GEMINI_REPAIR_MODELS`. `JOB_HUNT_PROVIDER_QUOTA_COOLDOWN_SECONDS` controls how long a key/model combination remains locally unavailable after a quota or authentication failure. The old tenant-level `gemini_model` configuration is no longer used for provider selection.

For the Mojtaba CV pipeline, project selection, project rewriting, work-experience selection, work-experience rewriting, skills tailoring, summary rewriting, cover-letter generation, job-page extraction, and qualification scoring remain separate model operations. Editing an active prompt template, temperature, or Output Structure in Baserow changes the next runtime execution without a code change.

## Local setup

1. Install [uv](https://docs.astral.sh/uv/) and Docker.
2. Run `uv sync --extra dev` and copy `.env.example` to `.env`.
3. Import the tenant configuration seed into a Baserow Configuration table and put its ID in `config/users.toml`.
4. Import the matching prompt seed into the tenant Prompts table. Prompt rows use `Prompt Key`, `Version`, `Prompt Template`, `Output Structure`, `Temperature`, `Status`, and `Enabled`; runtime uses only enabled rows whose status is `Active`.
5. Put the shared Apify and Gemini pools in the `JOB_HUNT_APIFY_TOKENS` and `JOB_HUNT_GEMINI_API_KEYS` environment variables. Keep tenant-specific Baserow, Cloudinary, Telegram, and Fillout credentials under each tenant's aliases.
6. Validate files with `uv run job-hunt config validate`; add `--live` after credentials and Baserow are ready.
7. Start the stack with `docker compose up --build -d`.
8. Run all offline checks with `make check`.

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

The adapters follow the official Apify Python client, Baserow database API, Cloudinary upload API, Telegram `sendMediaGroup`, Fillout webhooks, LangChain Google Generative AI integration, and Gemini structured-output contracts.
