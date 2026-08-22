# Operations and troubleshooting

## Run lifecycle

API calls return HTTP 202 and a UUID. Query `/v1/runs/{uuid}` or use `job-hunt status`. States are `queued`, `running`, `succeeded`, `failed`, or `skipped`. The `stage` field identifies the current/last workflow boundary. Notification state is separate from successful application generation.

Non-development environments emit structured logs. Active LLM generation logs include logical group, upstream provider/model when LiteLLM exposes it, deployment ID, latency, repair usage, and provider usage metadata.

## Discovery snapshots

Scheduled discovery loads tenant runtime configuration and active prompts once. The serialized snapshot is stored in Redis and attached to every child job in that batch. This avoids repeated Baserow reads and keeps one discovery batch on one prompt/configuration version.

If the snapshot expires before a child starts, the worker falls back to live Baserow configuration. `JOB_HUNT_DISCOVERY_SNAPSHOT_TTL_SECONDS` controls normal retention.

## LLM checkpoints

Validated structured LLM results are checkpointed in Redis by the active LiteLLM gateway client. The digest includes logical model group, prompt key, prompt version, rendered prompt, and JSON Schema.

Normal retry preserves the original checkpoint namespace. Fresh regeneration creates a new namespace. `JOB_HUNT_LLM_CHECKPOINT_TTL_SECONDS` controls retention.

## Active LLM routing

```text
application operation
  -> logical capability group
  -> LiteLLM Proxy
  -> concrete key/model deployment
```

Workers never select provider API keys directly. The current registry uses Gemini and Mistral.

Immediate LLM recovery belongs to LiteLLM:

```text
same-group deployments/keys
  -> configured fallback group
  -> that group's deployments/keys
  -> return error only after immediate routes fail
```

Current generation fallback is `job-powerful -> job-balanced -> job-fast`. Repair is `repair-fast -> repair-balanced`; `repair-balanced` already has Gemini, Mistral Medium, and Mistral Small capacity.

Celery is the outer recovery layer and retries only after the current LiteLLM request or another retryable provider/workflow operation escapes the task.

## Apify routing

Apify remains an application-wide token pool shared by tenants. Capacity errors cool the failing token and try other currently available tokens. If all tokens are already cooling down, the adapter returns a retryable rate-limit error with the shortest known remaining cooldown instead of sending another request to a known-unavailable token.

## Structured-output repair

Every active Baserow prompt provides an `Output Structure` JSON Schema. The worker parses and validates the provider result independently. Invalid JSON/schema output is sent to a repair capability group and validated again against the same schema.

## Common failures

| Error/stage | Likely cause | Check |
| --- | --- | --- |
| `configuration_error` | missing prompt/schema/group/table field/secret | `job-hunt config validate TENANT --live`, Baserow, `.env`, registry |
| `authentication` | invalid provider/tenant credential | `.env` and tenant secret aliases |
| `rate_limit` | current LiteLLM deployments or Apify tokens exhausted | LiteLLM/provider logs, cooldown state, Celery retry metadata |
| `malformed_provider_response` | generation and repair failed schema contract | prompt/version/schema and LiteLLM logs |
| `document_rendering` | invalid content/template/LaTeX | document-worker logs and generated JSON/TEX |
| notification failure | Telegram failure after generation | notification-worker logs; do not regenerate documents unnecessarily |
| URL validation | unsafe host/redirect/content/size | source posting URL and security logs |
| Redis readiness | Redis unavailable | `docker compose ps`, Redis logs |

## Safe reprocessing and cancellation

Automatic discovery is idempotent. Existing jobs are normally skipped before expensive LLM/document stages.

Forced reprocessing refreshes source job fields in Baserow, then qualification/document fields are replaced by their owning stages. Existing document attachments remain until replacement artifacts succeed.

A row manually changed to `Dropped` is treated as cancellation. Workers check status at expensive boundaries and stop further work where applicable.

## Monitoring

Flower is available on port 5555 and should be protected with `FLOWER_BASIC_AUTH` before exposure outside a trusted network.

LiteLLM is internal to the Compose network. Use its logs and `/v1/model/info` from another application container when investigating deployments or fallbacks.

## Recovery rules

- normal retry keeps the checkpoint namespace
- fresh regeneration creates a new checkpoint namespace
- tenant/job lock prevents concurrent processing of the same job identity
- LiteLLM should exhaust immediate deployment/fallback capacity before Celery retry
- Telegram retries are independent from generation
- do not run tests marked `live` with production provider credentials casually

## Deployment checks

Normal deployment:

```bash
bash scripts/deploy-vps.sh
```

Preview:

```bash
bash scripts/deploy-vps.sh --dry-run
```

Use explicit levels when the reason is not visible to Git, especially after a VPS-local `.env` change or after deliberately resetting the checkout before deployment.

- Level 0: pull only
- Level 1: mounted runtime/config refresh, no image build
- Level 2: lightweight application rebuild
- Level 3: shared Python/rendering/dependency/Docker/deployment changes

Run repository tests on the VPS with:

```bash
bash scripts/test-vps.sh
```
