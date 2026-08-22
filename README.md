# Job Hunt Automation

Python 3.12 multi-tenant automation for job discovery, qualification, tailored CV/cover-letter generation, Baserow persistence, and Telegram workflow controls.

This README is the operator/developer entry point. Detailed architectural decisions and known refactor debt are in [`docs/architecture-and-maintenance.md`](docs/architecture-and-maintenance.md).

## Runtime overview

```text
Fillout / operator API / CLI / scheduler
                 |
                 v
             FastAPI + Celery
                 |
        normalize canonical Job
                 |
          persist / dedupe
                 |
     compatibility -> qualification
                 |
          score threshold
                 |
          documents queue
                 |
        structured LLM tailoring
                 |
        JSON -> LaTeX -> PDF/ZIP
                 |
          Baserow + Telegram
```

Shared services:

- Redis for run/replay state, snapshots, locks, checkpoints, and cooldowns
- LiteLLM Proxy for provider/account deployment routing and immediate fallback
- Baserow for durable job state, runtime configuration, prompts, schemas, and documents
- Apify for LinkedIn discovery/single-job retrieval
- Telegram for progress and workflow actions

Current tenants are `mahsa` and `mojtaba`.

## Active LLM architecture

The production path is:

```text
Container
  -> integrations/llm_routing.py
  -> LiteLLM Proxy
  -> config/llm-providers.json
  -> provider deployment
```

Workers request logical groups, not provider API keys.

| Group | Current capacity | Typical operations |
| --- | --- | --- |
| `job-fast` | Gemini 3.5 Flash Lite + Mistral Medium | compatibility, page extraction |
| `job-balanced` | Gemini 3.5 Flash + Mistral Medium | qualification, selection, skills |
| `job-powerful` | Mistral Large | rewriting, summary, cover letter |
| `repair-fast` | fast deployments + Mistral Small | first repair attempt |
| `repair-balanced` | balanced deployments + Mistral Small | stronger repair |

Measured limits currently encoded in the registry:

| Model | RPM | TPM | Observed RPD |
| --- | ---: | ---: | ---: |
| Gemini 3.5 Flash Lite | 15 | 250,000 | 500 |
| Gemini 3.5 Flash | 5 | 250,000 | 20 |
| Mistral Medium | 50 | 25,000 | n/a |
| Mistral Large | 4 | 250,000 | n/a |
| Mistral Small | 50 | 50,000 | n/a |

Provider responses remain authoritative if account limits change. Groq is not part of the active registry.

### Retry/fallback order

Immediate alternatives are exhausted before the whole task is delayed. The generated LiteLLM router uses `simple-shuffle` with weighted deployment failover enabled, ordinary same-deployment retries disabled, and a failover budget derived from the generated deployment pool size.

```text
logical group
  -> chosen deployment/key
  -> failure excludes that deployment for this request
  -> another deployment/key in the same group
  -> configured fallback group
  -> deployments/keys in fallback group
  -> LiteLLM returns failure only after immediate capacity fails
  -> Celery outer retry
```

Generation fallback:

```text
job-powerful -> job-balanced -> job-fast
```

Repair fallback:

```text
repair-fast -> repair-balanced
```

`repair-balanced` already contains Gemini, Mistral Medium, and Mistral Small capacity.

Outer Celery defaults:

- max retries: 8
- rate-limit fallback: 65 seconds unless an adapter supplies `retry_after`
- transient backoff: 5, 10, 20, 40... seconds
- transient cap: 300 seconds

### Retry ownership and Baserow idempotency

Duplicate detection and task retry are intentionally different concepts.

- A fresh automatic discovery run treats an already-existing Baserow row as a duplicate and exits without spending compatibility, qualification, or document capacity.
- A Fillout/manual `force=True` request is an explicit fresh regeneration and may reset the existing row once before processing.
- A Celery retry of the same run is a resume, not a fresh discovery. After persistence, `process_submission` stores that run's Baserow `row_id` in Redis notification metadata. On retry, the workflow may resume only if the currently matching Baserow row has that exact recorded ID.
- A retry never claims ownership merely because a matching job exists. If the run-recorded row ID and the matching Baserow row disagree, the workflow fails rather than touching another run's row.
- A forced/manual retry does not reset the row, clear qualification, or set status back to `New` a second time.
- Manual `Dropped` status remains authoritative. Resuming a run still checks Baserow status at expensive stage boundaries and stops normally if the user dropped the row.

This distinction prevents the failure mode where a transient provider error occurs after persistence, Celery retries the task, and the task then mistakes its own newly created Baserow row for an unrelated duplicate.

## Structured output and checkpoints

Every active Baserow prompt has an `Output Structure` JSON Schema.

```text
LLM response
  -> parse JSON
  -> validate schema
  -> invalid? route through repair tier
  -> validate repaired result again
```

Validated results are checkpointed in Redis using a digest of logical group, prompt key/version, rendered prompt, and schema. Normal task retry keeps the checkpoint namespace; fresh regeneration creates a new namespace.

## Repository layout

```text
src/job_hunt/
  api/app.py                 HTTP and webhook boundaries
  application/               workflow/use-case logic
  domain/                    canonical models and job identity
  integrations/              external adapters and LLM routing
  rendering/                 renderer strategies and pdflatex
  config.py                  application/tenant configuration models
  container.py               dependency composition root
  ports.py                   provider-neutral protocols
  queueing.py                Celery enqueue adapter
  run_store.py               Redis run/replay store
  state.py                   snapshots, checkpoints, locks, cooldowns
  worker.py                  Celery task boundaries and progress

config/
  users.toml                 small tenant bootstrap registry
  llm-providers.json         committed provider/model registry
  seeds/                     Baserow seed files

tenants/
  mahsa/
  mojtaba/

docs/
  architecture-and-maintenance.md
  operations.md
  queue-architecture.md
  tenant-onboarding.md
  vps-deployment.md
```

## Dependency direction

Use the protocols in `ports.py` for application-facing dependencies. Important examples are `StructuredClient`, `JobExtractor`, `JobRepository`, `Qualifier`, `Tailor`, `DocumentRenderer`, and `DiscoveryProvider`.

The intended pattern is:

```text
application need -> protocol -> integration adapter -> Container wiring
```

Do not add provider key selection to workflow code. Provider/model/key ownership belongs to the LiteLLM provider registry and gateway.

## Job lifecycle

1. Intake arrives from Fillout, API, CLI, scheduler, LinkedIn ID, generic URL, or AI/content submission.
2. `SubmissionNormalizer` converts it to one canonical `Job`.
3. Job identity is assigned and Baserow is checked for duplicates.
4. Normal duplicate discovery work is skipped. Forced reprocessing refreshes source metadata while preserving previous documents until replacements succeed.
5. If a transient failure happens after persistence, a Celery retry resumes only the Baserow row ID recorded by that same run in Redis; it is not treated as a fresh duplicate.
6. Compatibility runs through `job-fast`.
7. Qualification runs through `job-balanced`.
8. Document generation is gated by `force` or `score >= qualification_threshold`.
9. Passing work is handed to the `documents` queue.
10. Independent LLM branches run with bounded parallelism.
11. JSON results are schema validated and repaired if required.
12. Tenant renderer code owns LaTeX structure; pdflatex creates PDFs.
13. Baserow receives persistent PDFs/ZIP.
14. Telegram finalization runs independently on the notifications queue.

## Configuration ownership

### `.env`

Secrets and process-level operational settings only. Provider keys are numbered, for example:

```text
GEMINI_API_KEY_1
GEMINI_API_KEY_2
MISTRAL_API_KEY_1
MISTRAL_API_KEY_2
```

Continue the sequence for independent accounts.

### `config/llm-providers.json`

Committed source for provider behavior:

- enabled state
- model assignments
- generation/repair groups
- RPM/TPM metadata
- provider-specific LiteLLM parameters
- optional discovery behavior

Do not commit live provider secrets here.

### `config/users.toml`

Only tenant bootstrap values needed before Baserow can be reached: renderer, configuration-table ID, asset root, and names of tenant Baserow/Fillout secret variables.

### Baserow Configuration table

Tenant-editable runtime settings such as table/form IDs, actor IDs, exclusions, qualification threshold, Telegram chat ID, and selection counts.

Historical rows such as `gemini_model` are ignored. LLM model routing is application-wide, not tenant-specific.

### Baserow Prompts table

Active rows define Prompt Key, Version, Prompt Template, Output Structure, Temperature, Status, and Enabled.

## Queues

| Queue | Work |
| --- | --- |
| `fast` | discovery, normalization, persistence, compatibility, qualification |
| `documents` | tailoring, rendering, artifact upload |
| `notifications` | Telegram progress/finalization |

Defaults:

```text
FAST_WORKER_CONCURRENCY=3
DOCUMENT_WORKER_CONCURRENCY=1
NOTIFICATION_WORKER_CONCURRENCY=1
JOB_HUNT_LLM_PARALLELISM=3
```

## Security boundaries

Generic URL intake must use `security.fetch_public_text`. It rejects non-HTTP schemes, credential-bearing URLs, private/reserved resolved addresses, unsafe redirects, oversized responses, and unsupported content types.

LinkedIn handling accepts only the actual `linkedin.com` host or its subdomains.

LaTeX compilation uses `-no-shell-escape`, and renderer code owns document structure.

Secrets belong in `.env` or a secret manager. Provider registry, tenant bootstrap, templates, and source code must remain secret-free.

## Development

```bash
uv sync --extra dev
cp .env.example .env
uv run job-hunt config validate
```

Quality checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Or:

```bash
make check
```

Live validation after provider/Baserow credentials and containers are ready:

```bash
uv run job-hunt config validate --live
```

Live validation checks tenant/Baserow contracts and that every configured LiteLLM logical group is exposed. Direct provider catalog validation is intentionally not duplicated in the CLI.

## CI

GitHub Actions runs lint, format check, mypy, tests, deployment-script syntax validation, Compose validation, application/document image builds, a generated LiteLLM smoke configuration using dummy Gemini and Mistral keys, logical-group checks, and a real Mahsa pdflatex compile.

The LiteLLM CI smoke environment must include dummy keys for every provider that is the sole source of a required capability group. `job-powerful`, for example, currently comes from Mistral.

## Deployment

Use the repository deployment script. Do not manually reproduce its Docker steps unless debugging the script itself.

Normal auto deployment:

```bash
cd /opt/job-hunt-automation
bash scripts/deploy-vps.sh
```

Preview:

```bash
bash scripts/deploy-vps.sh --dry-run
```

Important: run auto deployment before manually resetting/pulling the checkout. Auto mode compares the currently deployed commit to upstream to choose a level. If you deliberately run `git reset --hard origin/main` first, use an explicit level because the old changed-path information is gone.

### Deployment levels

| Level | Use when | Result |
| --- | --- | --- |
| 0 | docs/tests/non-runtime tracked changes | Git fast-forward only |
| 1 | mounted config/tenant assets or VPS-local `.env` | regenerate/recreate runtime services, no image build |
| 2 | narrow API/operator Python | rebuild lightweight application image |
| 3 | shared Python, workers, integrations, rendering, dependencies, Docker/Compose, deployment script | full application + document build/validation |

Examples:

```bash
bash scripts/deploy-vps.sh --level 1
bash scripts/deploy-vps.sh --level 2
bash scripts/deploy-vps.sh --level 3
```

When changes span levels, use the highest level.

## Debugging

```bash
docker compose ps
docker compose logs -f litellm
docker compose logs -f worker-fast
docker compose logs -f worker-documents
docker compose logs -f worker-notifications
```

Useful ownership map:

| Symptom | Inspect first |
| --- | --- |
| provider 429/cooldown | LiteLLM logs and provider dashboard |
| missing logical group | generated `config/litellm.runtime.yaml` and registry |
| malformed JSON | prompt schema, concrete provider test, repair group |
| Apify exhaustion | fast-worker logs and Redis cooldown state |
| retry becomes duplicate/skip | Redis run notification `row_id`, Baserow matching row, `job_persisted` `resumed` field |
| stale forced-job metadata | Baserow reset/source-field path |
| PDF failure | document worker and generated JSON/TEX |
| Telegram-only failure | notification worker |
| new provider key ignored | `.env`, Level 1 refresh, `/v1/model/info` |

## Design invariants

1. One shared application with narrow tenant differences.
2. Canonical `Job` is the boundary after normalization.
3. Application logic does not select provider keys.
4. Baserow is durable business state; Redis is runtime coordination.
5. Automatic processing is idempotent across independent runs; same-run Celery retries resume only the row ID recorded by that run.
6. Baserow status is user-owned.
7. Numeric qualification threshold gates documents.
8. LiteLLM owns immediate LLM fallback; Celery is the outer retry layer.
9. Generated and repaired structured output must pass schema validation.
10. Normal retries reuse checkpoints; fresh regeneration gets a new namespace.
11. Only dependency-independent LLM calls run concurrently.
12. Renderer code owns LaTeX structure.
13. Notification failure is not generation failure.
14. Last known-good documents survive failed regeneration.
15. Only the document worker carries TeX.

## Known refactor debt

The audit intentionally left several behavior-sensitive items for focused follow-up rather than changing them inside unrelated fixes:

- `worker.py` is too large and should be split into Celery config, retry/progress helpers, and task-family modules. It is currently exempted from Ruff formatting and coverage.
- historical `gemini.py`, `gemini_parallel.py`, and `gemini_mahsa.py` names remain even though production routing is multi-provider. Rename them as one mechanical refactor.
- URL canonicalization currently removes every query parameter. Changing this can alter existing stable job identities and needs a migration plan.
- Compose uses `ghcr.io/berriai/litellm:main-stable`; pin a tested version/digest in a dedicated deployment change.
- some historical provider dependencies remain while direct structured-client compatibility code still exists. Finish that cleanup together with the LLM module rename and regenerate `uv.lock` in the same change.

See [`docs/architecture-and-maintenance.md`](docs/architecture-and-maintenance.md) for the detailed rationale.
