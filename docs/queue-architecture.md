# Queue architecture

The pipeline is split by workload type so long document-generation tasks cannot block discovery/qualification and notification failures cannot consume document capacity.

## Queues

- `fast`: tenant dispatch, discovery, normalization, persistence, compatibility, qualification
- `documents`: tailored content generation, rendering, artifact upload
- `notifications`: Telegram progress, final delivery, and workflow actions

Default concurrency is conservative:

```text
FAST_WORKER_CONCURRENCY=3
DOCUMENT_WORKER_CONCURRENCY=1
NOTIFICATION_WORKER_CONCURRENCY=1
```

Document generation also uses bounded internal LLM parallelism through `JOB_HUNT_LLM_PARALLELISM`. Increase document worker concurrency only after checking provider quotas and VPS resources.

## LLM capacity ownership

Workers request logical LiteLLM groups. LiteLLM owns concrete provider/account deployment choice, immediate retries, cooldowns, and configured group fallbacks.

The application does not maintain duplicate Gemini-specific RPM/TPM/RPD counters. Current model RPM/TPM metadata lives in `config/llm-providers.json`; provider responses remain authoritative when real quota state differs.

Redis checkpoints are isolated by run lineage, while LiteLLM provider capacity is application-wide.

## Task handoff

`process_submission` persists and qualifies a job, then queues `generate_documents` and returns. It does not wait synchronously for documents.

`generate_documents` persists artifacts, queues notification finalization, and returns. Telegram delivery therefore cannot occupy document-worker capacity.

## Retry placement

Immediate provider/key/model failover occurs inside LiteLLM before a retryable error reaches Celery. Celery task retry is the outer layer after the gateway cannot find a usable immediate route.

Apify token rotation happens inside the Apify adapter before task-level retry for the same reason.

## Telegram delivery

The active workflow sends the generated application ZIP with action controls. Progress is maintained as an editable processing document/message. Notification state is independent from successful document persistence.
