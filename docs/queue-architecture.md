# Queue architecture

The job pipeline is deliberately split by workload type so long document-generation tasks cannot block discovery and qualification.

## Queues

- `fast`: tenant dispatch, discovery, normalization, persistence, qualification
- `documents`: CV and cover-letter generation, rendering, artifact upload
- `notifications`: Telegram delivery and workflow actions

The default worker concurrency is intentionally conservative:

- `FAST_WORKER_CONCURRENCY=3`
- `DOCUMENT_WORKER_CONCURRENCY=1`
- `NOTIFICATION_WORKER_CONCURRENCY=1`

Document generation already uses bounded internal LLM parallelism through `JOB_HUNT_LLM_PARALLELISM`, so document-task concurrency should only be increased after provider-rate observations support it.

## LLM capacity control

Gemini capacity is shared across tenants and workers. Provider cooldowns and optional RPM/TPM/RPD counters are stored in Redis, so they remain global even when checkpoints are isolated per run lineage.

Use `JOB_HUNT_GEMINI_LIMITS_JSON` to configure proactive per-account, per-model limits. Provider 429 responses also trigger candidate cooldown and key/model rotation.

## Task handoff

`process_submission` persists and qualifies a job, then queues `generate_documents` and returns immediately. It never waits synchronously for document generation.

`generate_documents` persists generated artifacts, queues `notify_documents`, and returns. Telegram delivery therefore cannot occupy document-generation worker capacity.

## Telegram grouping

A single file is sent with the workflow action buttons attached directly. Multiple files are sent as one media group, followed by a reply containing the action buttons. This is required because Telegram media groups do not support an inline keyboard on the media-group request itself.
