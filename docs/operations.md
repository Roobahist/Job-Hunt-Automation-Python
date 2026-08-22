# Operations and troubleshooting

## Run lifecycle

API calls return HTTP 202 and a UUID. Query `/v1/runs/{uuid}` or use `job-hunt status`. States are `queued`, `running`, `succeeded`, `failed`, or `skipped`. The `stage` field identifies the last workflow boundary. Notification delivery has its own `notification` state and does not change a successfully generated application back to failed.

Development logs are readable console events. Non-development environments emit structured JSON. Active LLM generation logs include the logical LiteLLM capability group, upstream provider/model when available, deployment ID, latency, repair usage, and provider usage metadata.

## Discovery snapshots

Scheduled discovery loads each tenant's runtime configuration and active Baserow prompts once. The serialized snapshot is stored in Redis and the same snapshot ID is attached to every child job from that discovery. This avoids repeated prompt/configuration reads and guarantees one discovery batch uses one prompt/configuration version.

If a snapshot expires before a child job starts, the worker falls back to live Baserow configuration rather than failing the job. `JOB_HUNT_DISCOVERY_SNAPSHOT_TTL_SECONDS` controls the normal snapshot lifetime.

## LLM checkpoints

Structured LLM operations may be checkpointed after schema validation. The checkpoint key is derived from the rendered prompt, prompt key, prompt version, and JSON Schema, and retries preserve the original checkpoint namespace unless a fresh regeneration is explicitly requested.

`JOB_HUNT_LLM_CHECKPOINT_TTL_SECONDS` controls retention for checkpoint-capable clients. Checkpoints contain generated structured content, so Redis persistence and access should be protected like the rest of the application data.

## Active LLM routing

The production path is centralized through LiteLLM Proxy:

```text
application workflow
    -> capability group selected in integrations/llm_routing.py
    -> LiteLLM Proxy
    -> concrete deployment from config/llm-providers.json
    -> provider account/model
```

Workers do not select individual provider API keys. Application code selects only logical capability groups such as `job-fast`, `job-balanced`, and `job-powerful`. LiteLLM owns deployment selection, retry within a group, cooldown, and configured fallbacks between groups.

The generated LiteLLM configuration creates one deployment for each configured provider model and API key. `allowed_fails` defaults to zero so a retryable failure cools the failed deployment immediately. The default `num_retries` is derived from the largest configured deployment pool, allowing alternative keys/models in that pool to be tried before an error is returned to Celery. `LITELLM_NUM_RETRIES` can override that derived value when necessary.

Celery is the outer recovery layer. It retries only after the LiteLLM request has failed at the gateway level or another retryable workflow/provider error escapes the current task. This separation prevents a single exhausted key from turning directly into a long Celery retry while healthy LiteLLM deployments remain available.

Apify remains an application-wide token pool shared by all tenants. Its cooldown state is stored in Redis so workers coordinate exhausted Apify accounts.

Legacy direct-Gemini pooling modules remain in the repository for migration/history, but they are not the active production routing path created by `Container`.

## Structured-output contract and repair

Every active Baserow prompt provides an `Output Structure` JSON Schema. The worker sends the schema as the output contract and validates the returned object independently.

If the primary result cannot be parsed or does not validate, the active routing layer sends a repair request through the configured repair capability group. The repaired object must pass the same JSON Schema. Repair is not permitted to invent facts that were absent from the original output.

## Common failures

| Error/stage | Likely cause | Check |
|---|---|---|
| `configuration_error` | Missing/duplicate prompt, invalid Output Structure, missing LiteLLM group, missing table field, or missing secret | `job-hunt config validate TENANT --live`, Baserow Prompts/Configuration tables, `.env`, `config/users.toml`, generated LiteLLM config |
| `authentication` | Expired or invalid provider credential | Provider keys in `.env`, tenant Baserow/Fillout credentials, shared Telegram credentials |
| `rate_limit` | LiteLLM exhausted currently usable deployments or Apify capacity is unavailable | LiteLLM logs, provider dashboards, generated deployment pools, Apify cooldown state, Celery retry metadata |
| `malformed_provider_response` | Structured generation and repair did not satisfy the active schema | Prompt key/version, Output Structure, LiteLLM/upstream logs, Langfuse trace when enabled |
| `document_rendering` | Invalid tailored content, template marker, or LaTeX | Run JSON/TEX files and document-worker logs |
| `notification.state=failed` | Telegram failed after application completion | Telegram token/connectivity; generation does not need to be repeated |
| URL validation | Non-public host, redirect, oversized/non-text response | Posting URL and response type |
| Redis readiness | Redis unavailable | `docker compose ps`, `docker compose logs redis` |

## Safe reprocessing and cancellation

Automatic discovery is idempotent. If a job row already exists, normal automatic processing does not spend compatibility, qualification, tailoring, or rendering capacity on it again. Manual/operator flows can use `force=true` to explicitly regenerate.

When forced reprocessing starts, qualification fields may be refreshed, but existing CV and cover-letter attachments are not cleared before replacement artifacts succeed. The previous documents therefore remain available if a later generation or upload fails.

Baserow status is treated as user-owned workflow state. If a row is manually changed to `Dropped`, workers check that state at expensive boundaries and stop further compatibility, qualification, tailoring, rendering, or upload work where applicable. A manual drop is a cancellation, not a workflow error.

Successful document persistence saves replacement artifact fields but does not implicitly advance a `New` row to another workflow status. Status transitions remain explicit through workflow/user actions rather than being coupled to artifact upload.

The ZIP sent through Telegram contains the generated JSON/TEX/PDF bundle. Baserow receives the persistent document attachments used by the job record.

## Monitoring

Flower is available on port 5555 in Docker Compose. Configure `FLOWER_BASIC_AUTH` before exposing it outside a trusted network. Celery task events are enabled so Flower can show queued/running/completed tasks and worker state.

LiteLLM is an internal service in Docker Compose and exposes its API only to the application network. Use its container logs when investigating routing, fallback, or upstream-provider failures.

Langfuse is optional. Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optionally `LANGFUSE_BASE_URL` to trace supported LLM calls. Leave the keys blank to disable it. Observability failures must not turn an otherwise successful LLM completion into a failed job.

## Telegram controls

The application uses one shared Telegram bot and one shared webhook secret for all tenants. Register the bot webhook at:

```text
/webhooks/telegram
```

Configure `JOB_HUNT_TELEGRAM_BOT_TOKEN` and `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET`. Telegram must send the same shared secret in `X-Telegram-Bot-Api-Secret-Token`.

Callback routing is resolved from the incoming Telegram `message.chat.id`. The application loads tenant runtime configuration and matches that chat ID against each tenant's Baserow `telegram_chat_id`. Tenant-specific Telegram webhook URLs and tenant-specific webhook secrets are not part of the current design.

Generated application messages can expose workflow actions such as opening the job, changing job status, or requesting regeneration. Telegram processing is isolated on the `notifications` queue so notification failures do not consume document-worker capacity or invalidate successfully generated application artifacts.

## Recovery rules

- Retry failed generation through the API, CLI, or Telegram regeneration action.
- Normal retry keeps the stored checkpoint namespace so already completed checkpoint-capable LLM operations can be reused.
- A fresh regeneration creates a new checkpoint namespace and forces the submission path.
- A tenant/job lock prevents concurrent processing of the same job identity.
- LiteLLM should exhaust healthy deployments before Celery performs task-level retry.
- Telegram retries happen independently from generation.
- Do not run tests marked `live` without explicit non-production provider credentials.

## Deployment checks

For normal VPS deployments use:

```bash
bash scripts/deploy-vps.sh
```

The script performs a fast-forward-only pull, builds the required images, regenerates `config/litellm.runtime.yaml`, recreates LiteLLM, verifies LiteLLM liveliness and configured capability groups, validates the tenant/application configuration, starts the workers, and checks API and Redis health.

Use `bash scripts/test-vps.sh` to run the repository test service against the current VPS checkout.
