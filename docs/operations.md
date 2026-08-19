# Operations and troubleshooting

## Run lifecycle

API calls return HTTP 202 and a UUID. Query `/v1/runs/{uuid}` or use `job-hunt status`. States are `queued`, `running`, `succeeded`, `failed`, or `skipped`. The `stage` field identifies the last workflow boundary. Notification delivery has its own `notification` state and does not change a successfully generated application back to failed.

Development logs are readable console events. Non-development environments emit structured JSON. Gemini generation events include prompt key/version, model, anonymized account identifier, latency, repair usage, and provider usage metadata when available.

## Discovery snapshots

Scheduled discovery loads each tenant's runtime configuration and active Baserow prompts once. The serialized snapshot is stored in Redis and the same snapshot ID is attached to every child job from that discovery. This avoids repeated prompt/configuration reads and guarantees one discovery batch uses one prompt/configuration version.

If a snapshot expires before a child job starts, the worker falls back to live Baserow configuration rather than failing the job. `JOB_HUNT_DISCOVERY_SNAPSHOT_TTL_SECONDS` controls the normal snapshot lifetime.

## LLM checkpoints

Each structured LLM operation is checkpointed after schema validation. The cache key contains the exact rendered prompt, prompt key, prompt version, and JSON Schema. Re-running a job therefore reuses unchanged successful stages while changed prompts, schemas, or job inputs naturally produce new checkpoint keys.

`JOB_HUNT_LLM_CHECKPOINT_TTL_SECONDS` controls retention. Checkpoints store structured generated content, so Redis persistence and access should be protected like the rest of the application data.

## Provider capacity

Apify and Gemini capacity are application-wide pools shared by all tenants. Each configured key/token is expected to represent a separate account. Redis stores provider cooldowns so every worker process sees the same exhausted account state.

Gemini uses model-first, account-second ordering for content generation. Proactive Redis counters track approximate RPM, TPM, and RPD usage per account/model using `JOB_HUNT_GEMINI_LIMITS_JSON`. These counters reduce predictable 429s but never override provider errors. When Gemini returns retry metadata, the actual retry window is preferred. Daily-limit errors fall back to the next midnight in the America/Los_Angeles timezone.

Structure-repair calls have an independent ordered Lite-model tier. A malformed response is validated against the exact Baserow schema, repaired, and validated again.

## Common failures

| Error/stage | Likely cause | Check |
|---|---|---|
| `configuration_error` | Missing/duplicate prompt, invalid Output Structure, unknown Gemini model, missing table field, or missing secret | `job-hunt config validate TENANT --live`, Baserow Prompts/Configuration tables, `.env`, `users.toml` |
| `authentication` | Expired or invalid provider credential | Shared Apify/Gemini pools or tenant Baserow/Telegram/Fillout credentials |
| `rate_limit` | All currently usable provider capacity is exhausted | Flower, Gemini/Apify dashboards, model order, Redis cooldown/counter keys |
| `malformed_provider_response` | Structured generation and repair did not satisfy the active schema | Prompt key/version, Output Structure, Langfuse trace, structured logs |
| `document_rendering` | Invalid tailored content, template marker, or LaTeX | Run JSON/TEX files and worker logs |
| `notification.state=failed` | Telegram failed after application completion | Telegram token/connectivity; generation does not need to be repeated |
| URL validation | Non-public host, redirect, oversized/non-text response | Posting URL and response type |
| Redis readiness | Redis unavailable | `docker compose ps`, `docker compose logs redis` |

## Safe reprocessing

Reprocessing refreshes the job fields, score, Apply value, and status, but it does not clear the existing CV or cover letter. The previous documents remain attached until the replacement PDFs have been generated and uploaded successfully. Successful document persistence moves the row to the configured `toApply` status.

The ZIP sent through Telegram contains the generated JSON/TEX/PDF bundle. Only the final CV and cover-letter PDFs are attached to Baserow, reducing storage and upload operations.

## Monitoring

Flower is available on port 5555 in Docker Compose. Configure `FLOWER_BASIC_AUTH` before exposing it outside a trusted network. Celery task events are enabled so Flower can show queued/running/completed tasks and worker state.

Langfuse is optional. Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optionally `LANGFUSE_BASE_URL` to trace LangChain calls. Leave the keys blank to disable it.

## Telegram controls

Generated applications receive an action message with Open Job, Mark Applied, Skip, and Regenerate controls. To enable callback actions, configure the tenant's `*_TELEGRAM_WEBHOOK_SECRET` and register `/webhooks/telegram/<tenant>` as the Telegram webhook using the same secret token. The webhook only accepts requests whose `X-Telegram-Bot-Api-Secret-Token` matches the tenant secret.

## Recovery rules

- Retry failed generation through the API, CLI, or Telegram Regenerate action.
- LLM checkpoints avoid repeating unchanged completed stages.
- A tenant/job lock prevents concurrent processing of the same job identity.
- Telegram retries happen independently from generation.
- Do not run tests marked `live` without explicit non-production provider credentials.
