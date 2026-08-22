# Job Hunt Automation

A Python 3.12 multi-tenant job discovery and application-generation system. It replaces the earlier Mahsa and Mojtaba n8n workflows with one shared application that accepts jobs, normalizes and deduplicates them, qualifies them with structured LLM calls, generates tailored CV and cover-letter artifacts, persists business state to Baserow, and reports progress through Telegram.

This README is the main codebase guide. It explains the active runtime architecture, the major design decisions, where different kinds of changes belong, how the workflow behaves, and how production deployments should be selected.

## Table of contents

- [System at a glance](#system-at-a-glance)
- [Active runtime architecture](#active-runtime-architecture)
- [Repository layout](#repository-layout)
- [End-to-end job lifecycle](#end-to-end-job-lifecycle)
- [Application boundaries and dependency direction](#application-boundaries-and-dependency-direction)
- [Tenant architecture](#tenant-architecture)
- [Configuration model](#configuration-model)
- [LLM architecture](#llm-architecture)
- [Discovery and normalization](#discovery-and-normalization)
- [Persistence and Baserow](#persistence-and-baserow)
- [Redis state and run tracking](#redis-state-and-run-tracking)
- [Celery and queue design](#celery-and-queue-design)
- [Document generation](#document-generation)
- [Telegram architecture](#telegram-architecture)
- [Error handling and retries](#error-handling-and-retries)
- [Security boundaries](#security-boundaries)
- [How to work with the codebase](#how-to-work-with-the-codebase)
- [Common extension workflows](#common-extension-workflows)
- [Testing strategy](#testing-strategy)
- [Local development](#local-development)
- [Production deployment](#production-deployment)
- [Deployment levels](#deployment-levels)
- [Operational debugging](#operational-debugging)
- [Design invariants](#design-invariants)
- [Legacy and migration code](#legacy-and-migration-code)

## System at a glance

The running system is composed of:

- **FastAPI** for Fillout webhooks, operator endpoints, run status, retries, health checks, and the shared Telegram callback endpoint.
- **Celery** for asynchronous discovery, normalization, qualification, document generation, and notifications.
- **Celery Beat** for scheduled tenant discovery dispatch.
- **Redis** for Celery transport/backend, run state, replay data, discovery snapshots, locks, cooldowns, and shared runtime coordination.
- **Baserow** as the business system of record for tenant configuration, prompt definitions, jobs, qualification results, statuses, and final PDF attachments.
- **Apify** for LinkedIn search and single-job retrieval.
- **LiteLLM Proxy** as the active LLM gateway. Application code asks for logical capability groups such as `job-fast`, `job-balanced`, and `job-powerful`; LiteLLM chooses concrete provider/model/key deployments and performs deployment failover.
- **Groq and Gemini** as currently configured upstream LLM providers. The registry is extensible.
- **LaTeX / pdfLaTeX** for deterministic CV and cover-letter rendering.
- **Telegram** for processing updates, completed bundles, and workflow actions.
- **Flower** for Celery visibility.
- **Langfuse** as optional tracing where supported by the active client path.

High-level flow:

```text
Fillout / Operator / Scheduler
            |
            v
         FastAPI
            |
            v
      RunCoordinator
            |
            v
        Celery fast
            |
     normalize job
            |
     persist / dedupe
            |
 compatibility filter
            |
       qualification
            |
      score threshold
        /       \
     drop       pass
                  |
                  v
          Celery documents
                  |
              tailoring
                  |
             LaTeX/PDF
                  |
          upload to Baserow
                  |
                  v
        Celery notifications
                  |
               Telegram
```

## Active runtime architecture

The current production LLM path is capability-routed through LiteLLM:

```text
Container
  -> build_routed_structured_client()
  -> CapabilityRoutedStructuredClient
  -> LiteLLMGatewayClient
  -> LiteLLM Proxy
  -> provider/model/key deployment
```

The application does not normally choose a concrete provider API key. It chooses an operation-level capability group. LiteLLM owns concrete deployment selection, cooldown, retries inside a logical group, and configured fallback between groups.

Default operation routing is approximately:

| Operation | Capability group |
| --- | --- |
| compatibility | `job-fast` |
| content extraction | `job-fast` |
| qualification | `job-balanced` |
| project selection | `job-balanced` |
| project rewrite | `job-powerful` |
| work selection | `job-balanced` |
| work rewrite | `job-powerful` |
| skills | `job-balanced` |
| summary | `job-powerful` |
| cover letter | `job-powerful` |
| structure repair | `repair-fast` |

`JOB_HUNT_LLM_OPERATION_GROUPS_JSON` can override operation-to-group mapping without putting provider names into workflow code.

## Repository layout

```text
.
├── config/
│   ├── llm-providers.json       # provider/model/key-prefix registry
│   ├── users.toml               # tenant bootstrap registry
│   └── seeds/                   # Baserow seed/reference CSVs
├── deploy/
│   └── nginx/                   # production reverse-proxy config
├── docs/
│   ├── operations.md
│   ├── queue-architecture.md
│   ├── tenant-onboarding.md
│   └── vps-deployment.md
├── scripts/
│   ├── deploy-vps.sh            # change-aware production deployment
│   ├── generate-litellm-config.py
│   ├── check-mahsa-latex.py
│   └── test-vps.sh
├── src/job_hunt/
│   ├── api/                     # HTTP boundary
│   ├── application/             # use cases and orchestration
│   ├── domain/                  # domain models and job identity
│   ├── integrations/            # provider/persistence adapters
│   ├── rendering/               # safe LaTeX rendering/compilation
│   ├── tenants/                 # tenant registry and renderer selection
│   ├── cli.py                   # operator CLI
│   ├── config.py                # typed settings/config models
│   ├── container.py             # composition root
│   ├── ports.py                 # protocol interfaces
│   ├── queueing.py              # queue adapter
│   ├── run_store.py             # run/replay state
│   ├── state.py                 # shared Redis primitives
│   └── worker.py                # Celery tasks and handoffs
├── tenants/
│   ├── mahsa/
│   │   ├── master_cv.json
│   │   └── templates/
│   └── mojtaba/
│       ├── master_cv.json
│       └── templates/
├── tests/
│   ├── unit/
│   ├── contract/
│   └── integration/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

### What belongs where

- Pure job concepts, state models, and identity rules: `domain/`
- Use-case sequencing and workflow decisions: `application/`
- Interfaces needed by application code: `ports.py`
- Baserow, Apify, Telegram, HTTP-provider, and LLM implementations: `integrations/`
- Dependency wiring: `container.py`
- HTTP-specific behavior: `api/`
- Celery task boundaries and queue handoffs: `worker.py`, `queueing.py`
- Tenant-specific files: `tenants/<key>/`
- Provider/model pools: `config/llm-providers.json`
- Tenant bootstrap information: `config/users.toml`

Do not fork the complete workflow per tenant unless the underlying data contract genuinely differs.

## End-to-end job lifecycle

### 1. Ingress

Jobs can enter through:

- `POST /webhooks/fillout/{tenant}`
- `POST /v1/tenants/{tenant}/jobs`
- `job-hunt submit`
- scheduled discovery
- operator-triggered discovery
- retry/regeneration from stored run data

Supported submission types are:

- `linkedin`: LinkedIn job ID
- `external`: already structured job fields
- `ai_content`: raw page content that must be extracted
- `url`: a LinkedIn or generic public job URL

HTTP requests return `202 Accepted` with a run UUID instead of waiting for scraping, LLM calls, or LaTeX.

**Reasoning:** these operations can outlive a safe HTTP request. A run ID makes the work observable and retryable.

### 2. Run creation and replay data

`RunCoordinator` creates a `RunStatus` in Redis, stores replay input separately, and enqueues a Celery task through the `Queue` protocol.

Retries create a new run linked to `original_run_id`. Normal retry can preserve the checkpoint namespace. Fresh regeneration uses a new namespace and forces the submission path.

### 3. Normalization

`SubmissionNormalizer` converts all ingress shapes to canonical `Job` objects.

- LinkedIn IDs use the configured Apify single-job actor.
- LinkedIn URLs are normalized to the same path.
- External submissions already contain canonical business fields.
- Generic URLs go through the public-text security boundary and structured extraction.
- Raw AI content goes directly to structured extraction.

Provider-specific field aliases are normalized centrally so downstream code does not branch on provider response shape.

### 4. Stable identity and deduplication

Each normalized job receives:

- stable textual identity
- deterministic integer internal ID
- canonical URL

External source IDs are preferred. Canonical URL is the fallback identity.

Deduplication happens both at discovery-batch level and at persistence level. Existing automatically discovered jobs are not requalified or regenerated unless processing is explicitly forced.

**Reasoning:** provider duplicates, overlapping discoveries, task redelivery, and retries mean exactly-once delivery cannot be assumed.

### 5. Persistence and cancellation

`ApplicationWorkflow.persist_and_qualify()` finds or creates the Baserow row.

Baserow is user-owned business state. A row manually set to `Dropped` is treated as a cancellation and checked at expensive stage boundaries.

**Reasoning:** automation must not override a user's explicit decision or keep spending quota after cancellation.

### 6. Compatibility filter

The compatibility filter is intentionally cheaper than the full application-generation pipeline. Incompatible jobs are dropped before expensive tailoring work.

### 7. Qualification

`qualification_scoring` returns structured score, `should_apply`, and reasoning.

Document gating is:

```text
passed = force OR score >= qualification_threshold
```

`should_apply` remains stored metadata; numeric threshold is the document-generation gate.

### 8. Queue handoff

Passing jobs move from the `fast` queue to the `documents` queue. The fast worker returns without waiting for document generation.

### 9. Tailoring

Mojtaba runs project, work-experience, and skills branches concurrently. Summary waits for those outputs, then the cover letter sees the final CV.

Mahsa runs work-experience, skills, and references-decision branches concurrently. Summary then consumes their results before the section-based CV is rebuilt.

`JOB_HUNT_LLM_PARALLELISM` bounds parallel work.

Only dependency-independent calls run concurrently.

### 10. Rendering and artifacts

The model does not write arbitrary LaTeX structure into final templates. It returns structured JSON, which reviewed renderer code maps to known LaTeX commands after escaping values.

Generated artifacts include:

- CV JSON
- CV TeX
- CV PDF
- cover-letter JSON
- cover-letter TeX
- cover-letter PDF
- ZIP bundle

Final PDFs persist to Baserow. Telegram receives the ZIP bundle.

### 11. Notifications

Telegram is isolated on the `notifications` queue. Notification failure cannot turn successful document generation into a failed business result.

## Application boundaries and dependency direction

The project uses a lightweight ports-and-adapters design.

`ports.py` defines protocols such as:

- `JobRepository`
- `CompatibilityFilter`
- `Qualifier`
- `Tailor`
- `DocumentRenderer`
- `ArtifactPublisher`
- `Notifier`
- `DiscoveryProvider`

`ApplicationWorkflow` depends on these protocols rather than provider SDKs. `Container` is the composition root that binds protocols to concrete implementations.

Preferred dependency direction:

```text
application need
    -> protocol
    -> integration adapter
    -> Container wiring
```

Do not import provider SDKs into `application/` or `domain/` unless there is a deliberate architectural reason.

## Tenant architecture

`config/users.toml` contains the small bootstrap set needed before Baserow can be reached:

- tenant key
- enabled flag
- renderer profile
- Baserow configuration table ID
- Baserow base URL
- tenant asset root
- names of environment variables containing tenant secrets

The larger tenant runtime configuration lives in Baserow and is validated into `TenantRuntimeConfig`.

Current renderer profiles:

- `mojtaba`: fixed top-level sections with project/work pipelines
- `mahsa`: dynamic section-oriented CV with required education and optional references

### Why configuration is split

The local bootstrap is intentionally small. Operator-editable values such as table IDs, search settings, thresholds, exclusions, actor IDs, and Telegram chat IDs belong in Baserow rather than being duplicated into code or many environment variables.

Secrets remain in `.env` or a deployment secret manager.

## Configuration model

There are four main layers.

### Application environment

`Settings` loads `JOB_HUNT_*` values from `.env`, including:

- Redis URL
- operator token
- timeouts
- Celery retry settings
- scheduler timezone
- LiteLLM logical routes
- artifact root
- shared Apify tokens
- Telegram credentials

### LiteLLM provider registry

`config/llm-providers.json` defines provider behavior without storing live keys:

- provider name and LiteLLM prefix
- numbered API-key prefix
- enabled state
- optional model discovery endpoint
- explicit fast/balanced/powerful model assignments
- discovery allowlist
- exclusions and blocklist
- provider-specific LiteLLM parameters

Numbered keys such as `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, `GEMINI_API_KEY_1`, and `GEMINI_API_KEY_2` are discovered from the environment.

### Tenant bootstrap

`config/users.toml` points each tenant at its configuration table, asset root, renderer, and tenant secret aliases.

### Tenant runtime configuration

The Baserow Configuration table controls jobs/search/prompts table IDs, option IDs, Fillout IDs, Apify actors, LinkedIn settings, exclusions, qualification threshold, Telegram chat ID, and selection counts.

## LLM architecture

### Capability groups

Application operations request logical capability rather than a hard-coded provider model:

```text
operation
   |
   v
job-fast / job-balanced / job-powerful
   |
   v
LiteLLM
   |
   v
provider + model + account
```

This keeps model/provider changes out of prompt orchestration.

### Runtime config generation

`scripts/generate-litellm-config.py` builds `config/litellm.runtime.yaml` from the provider registry and live environment.

Each model/account pair becomes a separate LiteLLM deployment.

### Retry ownership

LiteLLM is the first retry/failover layer for LLM calls. The generated router config:

- derives a default retry budget from the largest logical deployment pool
- defaults `allowed_fails` to `0`
- cools failed deployments
- defines capability fallback chains
- supports environment overrides such as `LITELLM_NUM_RETRIES`

Celery is the outer task-level retry layer. It should run only after the LLM gateway surfaces a retryable failure it could not resolve internally.

**Reasoning:** retrying a whole Celery task is much more expensive than trying another healthy model/key deployment inside the same LLM request.

### Structured-output contract

Baserow prompts contain:

```text
Prompt Key
Version
Prompt Template
Output Structure
Temperature
Status
Enabled
```

Only active/enabled prompt rows are loaded. `Output Structure` is JSON Schema. Returned model data is independently validated before downstream use.

Malformed structured output can go through a separate repair capability group, then must pass the same schema again.

## Discovery and normalization

Scheduled discovery reads active Search Criteria rows, constructs or reuses LinkedIn search URLs, calls the configured Apify search actor, normalizes results, applies company/title exclusions, deduplicates them, and queues canonical jobs.

Apify tokens form an application-wide pool shared by tenants. Quota/capacity failure cools a token and tries another available token. Redis-backed cooldown state coordinates workers.

## Persistence and Baserow

Baserow has two responsibilities:

1. user-facing durable business state
2. tenant-controlled configuration/prompt source

The Jobs table contract includes fields such as Job ID, Company Name, Title, Job Description, Link, Status, Score, Apply, CV, Cover Letter, Date, Location, and Contract Type.

Qualification writes score and Apply metadata separately from status transitions.

Replacement document generation does not clear the last known-good documents before the new artifacts succeed.

## Redis state and run tracking

`RunStore` stores run state, replay data, and notification progress.

Updates use Redis WATCH/MULTI transactions so concurrent workers do not silently overwrite each other's progress.

`RedisState` provides reusable primitives for snapshots, JSON state, checkpoints, cooldowns, and counters.

Redis is coordination/runtime state. It is not the long-term business source of truth.

## Celery and queue design

Queues are split by workload characteristics:

| Queue | Work |
| --- | --- |
| `fast` | discovery, normalization, persistence, compatibility, qualification |
| `documents` | tailoring, rendering, artifact upload |
| `notifications` | Telegram progress/final delivery |

Default concurrency is conservative:

```text
FAST_WORKER_CONCURRENCY=3
DOCUMENT_WORKER_CONCURRENCY=1
NOTIFICATION_WORKER_CONCURRENCY=1
```

`worker_prefetch_multiplier=1` prevents one worker from reserving a large set of long jobs.

## Document generation

The document worker is intentionally isolated because it needs TeX packages and document compilation while the other runtime processes do not.

### Image design

The production image topology is now:

```text
lightweight application image
  -> api
  -> worker-fast
  -> worker-notifications
  -> beat
  -> flower

document image with TeX
  -> worker-documents
```

Only `worker-documents` carries the LaTeX packages.

The TeX installation is kept in a stable Docker layer. Rebuilding the document image after a source-only change can reuse that layer unless the TeX package list or an earlier Dockerfile stage changed.

**Reasoning:** the TeX toolchain is large and slow to install. Non-document services should not pay that cost.

## Telegram architecture

The application uses one shared bot and one shared callback endpoint:

```text
POST /webhooks/telegram
```

`JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` authenticates webhook requests. Tenant routing uses the incoming chat ID and each tenant's configured `telegram_chat_id`.

Telegram actions can update Baserow status or request regeneration.

## Error handling and retries

Errors are classified instead of being treated as generic exceptions. Important categories include:

- validation
- business condition
- authentication
- rate limit
- transient provider failure
- malformed provider response
- configuration failure
- document rendering failure

Retry ownership should stay explicit:

- deployment/model/key failover: LiteLLM or provider adapter
- whole-stage transient failure: Celery
- invalid business/config input: no retry
- notification failure: notifications queue only

## Security boundaries

### Secrets

Secrets belong in `.env` or a secret manager. Committed files contain secret aliases/prefixes, not live credentials.

### Operator API

Operator endpoints require the configured bearer token.

### Fillout

Each tenant has its own Fillout bearer secret and expected form ID.

### Telegram

The shared webhook secret is compared using constant-time comparison.

### Generic URLs

Generic URL ingestion must continue through the public-text fetch security boundary. Do not replace it with unrestricted server-side HTTP fetches.

### LaTeX

Model output is data, not trusted template source. Preserve escaping and renderer-owned structure.

## How to work with the codebase

Before making a change, identify the owning layer.

| Change | Primary location |
| --- | --- |
| new job invariant | `domain/` |
| workflow sequencing/gating | `application/` |
| new external provider adapter | `integrations/` |
| dependency wiring | `container.py` |
| new HTTP behavior | `api/` |
| async task/queue boundary | `worker.py`, `queueing.py` |
| renderer/document structure | `rendering/` |
| tenant assets | `tenants/<key>/` |
| provider/model pool | `config/llm-providers.json` |
| application setting | `config.py`, `.env.example` |
| tenant-editable setting | Baserow Configuration contract/seed |

### Preserve provider neutrality

If `ApplicationWorkflow` needs a new external capability, add or extend a protocol and inject an implementation. Do not instantiate SDK clients inside orchestration code.

### Preserve business/runtime state separation

Use Baserow for durable user-owned workflow data. Use Redis for runtime coordination and TTL-bound execution state.

### Preserve idempotency

Assume duplicate provider records, overlapping discovery windows, task redelivery, and partial retries.

### Preserve cancellation checks

When adding expensive work after a Baserow row exists, consider checking whether it has been manually dropped before and after that work.

### Treat retry placement as architecture

Do not add retries at every layer. Decide which component owns the failure first.

## Common extension workflows

### Add a tenant using an existing renderer

1. Create `tenants/<key>/master_cv.json`.
2. Add CV and cover-letter templates.
3. Add `[users.<key>]` to `config/users.toml`.
4. Import/create the Baserow Configuration rows.
5. Import the matching prompt contract.
6. Configure tenant Baserow and Fillout secrets.
7. Set Telegram chat ID in Baserow.
8. Run static and live config validation.
9. Test one manual job, one threshold-gated job, and one discovery.

### Add a new renderer profile

1. Define the master-CV JSON contract.
2. Add a `CvRenderer` implementation.
3. Add a cover-letter strategy only if needed.
4. Extend `TenantRegistry` selection.
5. Define required prompt keys.
6. Update container validation/tailoring strategy.
7. Add renderer unit tests and compile-level integration coverage.

### Add an LLM provider

Usually no application Python change is required.

1. Add a provider object to `config/llm-providers.json`.
2. Choose a unique numbered API-key prefix.
3. Configure explicit groups or model discovery.
4. Add numbered keys to the VPS environment.
5. Regenerate LiteLLM runtime config.
6. Run live capability validation.
7. Add registry/config-generation tests if the provider introduces new behavior.

### Add an LLM operation

1. Add the Baserow prompt and JSON Schema.
2. Add the prompt key to the appropriate required contract if always required.
3. Implement prompt rendering/orchestration.
4. Choose the default capability group or add an environment override.
5. Validate output before downstream use.
6. Add tests for prompt rendering and output assumptions.

### Add a submission type

1. Add a discriminated Pydantic submission model.
2. Add it to `JobSubmission`.
3. Extend `SubmissionNormalizer`.
4. Update Fillout mapping only if Fillout can produce the new type.
5. Add API and normalization tests.
6. Keep the result as canonical `Job`.

### Add a Baserow field

Decide whether it is a required infrastructure contract, optional business metadata, or tenant configuration. Update the typed model/repository mapping/tests accordingly. Required job fields belong in `REQUIRED_JOB_FIELDS` so live validation fails early.

### Add a Celery stage

Create a new queue only if workload characteristics justify separate concurrency or failure isolation. Otherwise keep the stage inside an existing task and expose progress boundaries.

## Testing strategy

The repository uses unit, contract, and integration tests.

### Unit tests

Use for domain rules, configuration parsing, workflow decisions, rendering, run state, and provider-selection/config-generation logic.

### Contract tests

Use when the important question is whether an adapter speaks the expected external contract.

### Integration tests

Use for local infrastructure such as Redis or real pdfLaTeX.

Tests marked `live` may call configured external services and should not be run casually against production credentials.

### Quality gates

CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

CI also validates deployment-script shell syntax, Docker Compose configuration, the API image, LiteLLM smoke routing, the TeX-enabled document image, and a real Mahsa LaTeX compile.

## Local development

Requirements:

- Python 3.12+
- `uv`
- Docker Engine / Docker Compose

Setup:

```bash
uv sync --extra dev
cp .env.example .env
```

Validate local tenant assets:

```bash
uv run job-hunt config validate
```

Validate live Baserow/LiteLLM configuration:

```bash
uv run job-hunt config validate --live
```

Start the stack:

```bash
docker compose up --build -d
```

Useful logs:

```bash
docker compose logs -f api
docker compose logs -f worker-fast
docker compose logs -f worker-documents
docker compose logs -f worker-notifications
docker compose logs -f litellm
```

CLI examples:

```bash
uv run job-hunt submit mahsa --input examples/job.json
uv run job-hunt submit mahsa --input examples/job.json --force
uv run job-hunt discover mojtaba
uv run job-hunt status RUN_UUID
uv run job-hunt retry RUN_UUID
```

## Production deployment

The supported VPS entry point is:

```bash
bash scripts/deploy-vps.sh
```

Do not replace it with an ad hoc sequence of `docker compose down`, `build`, and `up` unless debugging the deployment mechanism itself.

The script now performs change-aware deployment. It fetches the upstream branch, compares the current VPS commit with the upstream commit, selects the lowest safe deployment level, fast-forwards the checkout, and performs only the actions required by that level.

Preview the decision without changing the checkout or containers:

```bash
bash scripts/deploy-vps.sh --dry-run
```

Force a deployment level:

```bash
bash scripts/deploy-vps.sh --level 0
bash scripts/deploy-vps.sh --level 1
bash scripts/deploy-vps.sh --level 2
bash scripts/deploy-vps.sh --level 3
```

## Deployment levels

Deployment levels are part of the development workflow. After every code/config change, identify the required level. Auto mode should normally be used, but the level should still be stated in handoff/deployment instructions.

### Level 0: pull-only

Use for changes that do not affect the running application:

- `README.md`
- `docs/**`
- `tests/**`
- `.env.example`
- `config/seeds/**`

Actions:

```text
fetch
fast-forward checkout
no Docker build
no container restart
no LiteLLM reload
no LaTeX validation
```

Example:

```bash
bash scripts/deploy-vps.sh --level 0
```

In normal auto mode these changes finish after the Git fast-forward.

### Level 1: runtime refresh, no image build

Use for runtime files that are mounted from the host or for VPS-local environment changes:

- `config/users.toml`
- `config/llm-providers.json`
- `tenants/**`
- VPS-local `.env` changes

Actions as needed:

- no application/document image build
- reload generated LiteLLM configuration when provider/runtime config requires it
- run LaTeX validation when document assets require it
- recreate application services so mounted runtime state is refreshed
- run health checks

For VPS-local `.env` changes Git cannot detect the reason, so explicitly use:

```bash
bash scripts/deploy-vps.sh --level 1
```

### Level 2: application rebuild

Use for code that affects only the lightweight application/operator boundary and does not require rebuilding document-worker code. Auto mode currently recognizes paths such as:

- `src/job_hunt/api/**`
- `src/job_hunt/cli.py`
- `src/job_hunt/queueing.py`
- `scripts/generate-litellm-config.py`

Actions:

- rebuild `job-hunt-automation-api:latest`
- restart API, fast worker, notification worker, Beat, and Flower
- leave `worker-documents` on its existing TeX-enabled image
- reload/validate LiteLLM
- run API/Redis health checks

This is the main optimization for ordinary API/operator changes.

### Level 3: full deployment

Use for changes that affect shared worker code, document generation, dependencies, build topology, or anything where the document worker must receive new Python code:

- most `src/**` changes outside the narrow Level 2 paths
- `src/job_hunt/rendering/**`
- `src/job_hunt/worker.py`
- `src/job_hunt/container.py`
- shared `application/`, `domain/`, and `integrations/` changes
- `pyproject.toml`
- `uv.lock`
- `Dockerfile`
- `docker-compose.yml`
- `scripts/deploy-vps.sh`
- uncertain runtime/build changes

Actions:

- build application and document targets in one Compose build invocation
- reuse cached TeX layers whenever possible
- regenerate/restart/validate LiteLLM
- run LaTeX validation
- recreate all application services
- run API and Redis health checks

Use:

```bash
bash scripts/deploy-vps.sh --level 3
```

### Why the levels exist

The old deployment path rebuilt the API and TeX-enabled worker image and ran all validations on every deploy, even for documentation-only commits. That was safe but unnecessarily expensive.

The new design separates deployment cost by change type:

```text
docs only
   -> Git pull

mounted config / tenant assets
   -> runtime refresh without build

API/operator code
   -> lightweight app rebuild

shared worker/rendering/build changes
   -> full rebuild
```

Only the document worker carries TeX. Fast workers, notification workers, API, Beat, and Flower reuse the lightweight API image.

### Auto mode and overrides

Default:

```bash
bash scripts/deploy-vps.sh
```

Auto mode intentionally errs toward the safer level for unknown/shared runtime files.

Use `--dry-run` before deployment when you want to inspect the selected level and changed paths:

```bash
bash scripts/deploy-vps.sh --dry-run
```

Use an explicit level when:

- the change exists only in VPS-local `.env`
- you deliberately want a full validation/rebuild
- you are recovering from a failed or partially migrated deployment
- the deployment topology itself just changed

### Deployment-level reporting rule

For future repository changes, the handoff should state the required deployment level explicitly, for example:

```text
Deployment level: 2 (application rebuild)
Reason: only FastAPI/operator code changed; document-worker image is unaffected.
Command: bash scripts/deploy-vps.sh --level 2
```

If several changed files require different levels, use the highest required level.

For normal ongoing work, auto mode is still preferred after the level is understood:

```bash
bash scripts/deploy-vps.sh
```

### First deployment after this optimization

The Docker/Compose topology and deployment script itself changed as part of this optimization, so the first VPS deployment that adopts it must be **Level 3**. After that one migration deploy, return to normal auto mode.

If the VPS still has the old deployment script checked out, update the checkout first and then invoke the new script explicitly:

```bash
git pull --ff-only
bash scripts/deploy-vps.sh --level 3
```

Subsequent deployments can simply use:

```bash
bash scripts/deploy-vps.sh
```

## Operational debugging

Check services:

```bash
docker compose ps
```

Check API:

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

Check LiteLLM:

```bash
docker compose logs --tail=200 litellm
```

Check workers independently:

```bash
docker compose logs --tail=200 worker-fast
docker compose logs --tail=200 worker-documents
docker compose logs --tail=200 worker-notifications
```

Check a run:

```bash
uv run job-hunt status RUN_UUID
```

Typical ownership:

| Symptom | Inspect first |
| --- | --- |
| provider/model 429 | LiteLLM logs and generated deployment pool |
| all Apify accounts exhausted | fast worker and Redis cooldown state |
| malformed structured output | prompt schema, LiteLLM logs, repair route |
| missing Baserow fields | live configuration validation |
| duplicate job skip | stable identity and existing Baserow row |
| dropped job still doing work | cancellation checks around new stage |
| PDF failure | document worker, generated JSON/TEX, template markers |
| Telegram failure after PDFs | notification worker only |
| new model/key not active | regenerated `litellm.runtime.yaml` and LiteLLM recreation |
| slow deployment | inspect selected deployment level with `--dry-run` |

## Design invariants

Treat these as architectural constraints unless there is a deliberate migration plan:

1. One shared application, narrow tenant differences.
2. Application logic depends on protocols, not provider SDKs.
3. Canonical `Job` is the boundary after normalization.
4. Automatic processing is idempotent.
5. Baserow status is user-owned.
6. Numeric qualification threshold gates document generation.
7. LLM code asks for capability groups, not concrete provider keys.
8. LiteLLM handles immediate deployment failover; Celery is the outer task retry layer.
9. Structured LLM output is independently schema validated.
10. Only dependency-independent tailoring calls run in parallel.
11. Generated content does not control LaTeX structure.
12. Notification failure is not generation failure.
13. Last known-good documents survive failed regeneration.
14. Queue separation reflects workload isolation.
15. Provider capacity is shared infrastructure.
16. Deployment regenerates LiteLLM runtime config when required.
17. Secrets stay outside committed configuration.
18. Only the document worker carries TeX.
19. Deployment level must match the highest-impact changed file.
20. Documentation-only changes must not trigger application rebuilds.

## Legacy and migration code

The repository still contains modules from the earlier direct-Gemini routing architecture, including `gemini_pool.py`, `gemini_catalog.py`, and related compatibility code/tests.

The active production composition path is defined by `Container` and currently follows:

```text
container.py
  -> integrations/llm_routing.py
  -> integrations/litellm_config.py
  -> config/llm-providers.json
  -> LiteLLM Proxy
```

Do not add new production behavior to retained direct-Gemini pool code simply because a similarly named module or test exists. Confirm what `Container`, `worker.py`, and Docker Compose actually instantiate.

## Additional documentation

- `docs/operations.md`: run lifecycle and troubleshooting
- `docs/queue-architecture.md`: queue isolation rationale
- `docs/tenant-onboarding.md`: tenant setup checklist
- `docs/vps-deployment.md`: deployment levels and VPS-specific deployment notes

When documentation disagrees with executable wiring, treat the composition root, runtime configuration, and Compose topology as the source of truth, then update the stale documentation as part of the same change.
