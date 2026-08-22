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

Workers request logical LiteLLM groups. LiteLLM owns concrete provider/account deployment choice, weighted deployment failover, cooldowns, and configured group fallbacks.

The application does not maintain duplicate Gemini-specific RPM/TPM/RPD counters. Current model RPM/TPM metadata lives in `config/llm-providers.json`; provider responses remain authoritative when real quota state differs.

Redis checkpoints are isolated by run lineage, while LiteLLM provider capacity is application-wide.

## Task handoff

`process_submission` persists and qualifies a job, then queues `generate_documents` and returns. It does not wait synchronously for documents.

`generate_documents` persists artifacts, queues notification finalization, and returns. Telegram delivery therefore cannot occupy document-worker capacity.

## Retry placement

Immediate provider/key/model failover occurs inside LiteLLM before a retryable error reaches Celery. Celery task retry is the outer layer after the gateway cannot find a usable immediate route.

Apify token rotation happens inside the Apify adapter before task-level retry for the same reason.

### Persisted-row ownership on `process_submission` retry

A retry of `process_submission` is not a fresh duplicate check. Persistence records the Baserow `row_id` in that run's Redis notification metadata. On a later Celery retry, the task passes that same-run row ID into the application workflow and may resume only if the matching Baserow row has the exact same ID.

This gives the queue boundary two separate idempotency rules:

```text
fresh independent run + existing job
  -> duplicate skip

same Celery run + recorded persisted row
  -> resume compatibility/qualification
```

A retry never infers ownership merely from a matching job identity. If the run-recorded row and Baserow disagree, processing fails instead of touching another run's row. Forced/manual retries also resume without repeating reset, qualification clearing, or `New` status mutation.

The current failing substage is tracked by the worker progress callback, so a compatibility timeout is deferred/logged as `compatibility_filter` rather than the broader `qualification` stage.

## Telegram delivery

The active workflow sends the generated application ZIP with action controls. Progress is maintained as an editable processing document/message. Notification state is independent from successful document persistence.
