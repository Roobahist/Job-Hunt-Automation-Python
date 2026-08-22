# Job Hunt Automation

A Python 3.12 multi-tenant job discovery and application-generation system. It replaces the earlier Mahsa and Mojtaba n8n workflows with one shared application that accepts jobs, normalizes and deduplicates them, qualifies them with structured LLM calls, generates tailored CV and cover-letter artifacts, persists business state to Baserow, and reports progress through Telegram.

This README is the codebase guide. It explains how the runtime is assembled, where different kinds of changes belong, the major design decisions, and the invariants that should be preserved when extending the system.

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
- [Operational debugging](#operational-debugging)
- [Design invariants](#design-invariants)
- [Legacy and migration code](#legacy-and-migration-code)

## System at a glance

The running system is composed of:

- **FastAPI** for Fillout webhooks, operator endpoints, run status, retries, health checks, and the shared Telegram callback endpoint.
- **Celery** for asynchronous discovery, normalization, qualification, document generation, and notifications.
- **Celery Beat** for scheduled tenant discovery dispatch.
- **Redis** for Celery transport/backend, run state, replay data, discovery snapshots, locks, cooldowns, and other shared ephemeral coordination state.
- **Baserow** as the business system of record for tenant configuration, prompt definitions, jobs, qualification results, statuses, and final PDF attachments.
- **Apify** for LinkedIn search and single-job retrieval.
- **LiteLLM Proxy** as the active LLM gateway. Application code asks for logical capability groups such as `job-fast`, `job-balanced`, and `job-powerful`; LiteLLM chooses concrete provider/model/key deployments and performs deployment failover.
- **Groq and Gemini** as currently configured upstream LLM providers. The provider registry is extensible.
- **LaTeX / pdfLaTeX** for deterministic CV and cover-letter rendering.
- **Telegram** for processing updates, completed bundles, and workflow actions.
- **Flower** for Celery task visibility.
- **Langfuse** as optional tracing infrastructure where supported by the active client path.

At a high level:

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

The most important architectural fact is that the current runtime is **capability routed through LiteLLM**.

The active dependency path is:

```text
Container
  -> build_routed_structured_client()
  -> CapabilityRoutedStructuredClient
  -> LiteLLMGatewayClient
  -> internal LiteLLM Proxy
  -> provider/model/key deployment
```

The application does not normally choose a concrete Gemini or Groq API key. It selects an operation-level capability group. LiteLLM owns deployment selection, cooldown, retries, and fallback among concrete deployments.

Examples of default operation routing:

| Operation | Default capability group |
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

These mappings are defaults, not hard-coded provider choices. `JOB_HUNT_LLM_OPERATION_GROUPS_JSON` can override operation-to-group routing without changing application code.

## Repository layout

```text
.
├── config/
│   ├── llm-providers.json       # provider discovery, model pools, key prefixes
│   ├── users.toml               # tenant bootstrap registry
│   └── seeds/                   # Baserow configuration and prompt seed CSVs
├── deploy/
│   └── nginx/                   # production reverse-proxy configuration
├── docs/
│   ├── operations.md
│   ├── queue-architecture.md
│   ├── tenant-onboarding.md
│   └── vps-deployment.md
├── scripts/
│   ├── deploy-vps.sh            # normal production deployment entry point
│   ├── generate-litellm-config.py
│   ├── check-mahsa-latex.py
│   └── test-vps.sh
├── src/job_hunt/
│   ├── api/                     # HTTP boundary
│   ├── application/             # use cases and orchestration
│   ├── domain/                  # domain models and job identity rules
│   ├── integrations/            # provider and persistence adapters
│   ├── rendering/               # safe LaTeX rendering and compilation
│   ├── tenants/                 # tenant registry and renderer selection
│   ├── cli.py                   # operator CLI
│   ├── config.py                # typed settings and tenant config models
│   ├── container.py             # composition root / dependency wiring
│   ├── ports.py                 # protocol interfaces
│   ├── queueing.py              # Celery queue adapter
│   ├── run_store.py             # run state and replay storage
│   ├── state.py                 # shared Redis coordination primitives
│   └── worker.py                # Celery tasks and queue handoffs
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

Use the layer boundaries rather than putting new behavior wherever it is easiest to access a dependency.

- Put **pure job concepts, state models, and identity rules** in `domain/`.
- Put **use-case sequencing** in `application/`.
- Put **interfaces required by application code** in `ports.py`.
- Put **Baserow, Apify, Telegram, HTTP, and LLM implementation details** in `integrations/`.
- Put **construction and dependency wiring** in `container.py`.
- Put **HTTP-specific behavior** in `api/`.
- Put **Celery task boundaries, queue routing, task retries, and asynchronous handoffs** in `worker.py` and `queueing.py`.
- Put **tenant-specific file assets** under `tenants/<key>/`.
- Add Python tenant-specific behavior only when the tenant really has a different data/rendering contract. Do not fork the whole workflow per tenant.

## End-to-end job lifecycle

### 1. Ingress

Jobs can enter through several paths:

- `POST /webhooks/fillout/{tenant}`
- `POST /v1/tenants/{tenant}/jobs`
- `job-hunt submit`
- scheduled or operator-triggered discovery
- retry/regeneration from stored run data

The domain supports four submission types:

- `linkedin`: a LinkedIn job ID
- `external`: already structured job fields
- `ai_content`: raw page content that must be extracted into a job
- `url`: a URL that is either recognized as LinkedIn or safely fetched and extracted

The HTTP layer validates authentication and request shape, then delegates to `RunCoordinator`. The API intentionally returns `202 Accepted` with a run UUID instead of executing the workflow inside the request.

**Reasoning:** provider calls, scraping, LLM generation, and LaTeX can take much longer than a safe HTTP request lifetime. A run ID makes execution observable and retryable without holding client connections open.

### 2. Run creation and replay data

`RunCoordinator` creates a `RunStatus` in Redis and separately stores the replay request. It then asks the `Queue` protocol to enqueue either a submission or discovery task.

A retry creates a new run linked through `original_run_id`. For ordinary retry, the stored checkpoint namespace can be retained. A fresh regeneration uses a new namespace and forces reprocessing.

**Reasoning:** run state and replay input are separate concepts. The user can inspect historical execution state while the system still has enough information to enqueue a new attempt.

### 3. Normalization

`SubmissionNormalizer` converts every supported input into the canonical `Job` model.

- LinkedIn IDs are fetched using the tenant's single-job Apify actor.
- LinkedIn URLs are recognized and converted to the same single-job path.
- External jobs already contain canonical business fields.
- Generic URLs are fetched through the public-text security boundary and then sent to the extraction operation.
- Raw AI content is sent directly to extraction.

Provider field variants such as `jobId`, `job_id`, `jobTitle`, `job_title`, `publishedAt`, and similar aliases are normalized centrally.

**Reasoning:** downstream workflow code should not care how a job entered the system or how a provider happened to name a field.

### 4. Stable identity and deduplication

Every normalized `Job` receives:

- a stable textual `identity`
- a deterministic integer `internal_id`
- a canonical URL

If the source supplies a stable external ID, identity is `source:external_id`. Otherwise it falls back to the canonicalized URL. The internal ID is derived from SHA-256 and limited to JavaScript-safe integer range.

Automatic discovery also deduplicates the batch by identity before jobs are queued.

The workflow performs a second persistence-level duplicate check in Baserow. Existing automatically discovered jobs are not requalified or regenerated unless processing is explicitly forced.

**Reasoning:** discovery runs can overlap, providers can return the same posting through different searches, and worker delivery is not exactly-once. Idempotency therefore exists at more than one layer.

### 5. Persistence

`ApplicationWorkflow.persist_and_qualify()` asks `JobRepository` to find or create the job row.

Baserow is treated as user-owned business state. In particular, a manually selected `Dropped` status is checked before and after expensive stages.

**Reasoning:** if a user cancels a job while a worker is running, the automation should stop spending provider quota rather than overwrite the user's decision later.

### 6. Compatibility filter

Unless force-processing bypasses it, the compatibility filter performs a relatively cheap structured LLM check before the full qualification and tailoring workload.

An incompatible job is marked `Dropped` without entering document generation.

**Reasoning:** reject obvious mismatches before spending stronger-model capacity on them.

### 7. Qualification

The `qualification_scoring` prompt returns structured data containing a score, `should_apply`, and reasoning.

Document gating is based on the numeric qualification threshold:

```text
passed = force OR score >= qualification_threshold
```

`should_apply` is persisted as metadata in Baserow but is not the threshold calculation itself.

If a non-forced job fails the threshold, its status becomes `Dropped`.

### 8. Document queue handoff

A job that passes qualification is handed from the `fast` queue to the `documents` queue. The fast worker does not wait for document generation.

**Reasoning:** discovery and qualification should remain responsive even when a LaTeX compile or long LLM call occupies a document worker.

### 9. Tailoring

Each tenant uses a shared workflow interface but a profile-specific tailoring pipeline.

For Mojtaba, independent project, work-experience, and skills branches execute concurrently. Summary generation waits for those outputs. The cover letter then receives the completed tailored CV.

For Mahsa, work-experience tailoring, skills tailoring, and the references decision execute concurrently. Summary generation then uses the completed outputs before the section-based CV is rebuilt.

`JOB_HUNT_LLM_PARALLELISM` bounds the thread pool used inside one tailoring task.

**Reasoning:** only dependency-independent calls are parallelized. Dependent generation remains ordered, which reduces latency without making prompt data flow ambiguous.

### 10. Rendering and artifacts

The LLM never writes arbitrary LaTeX structure directly into the final template.

Structured JSON is rendered by explicit profile renderers. Text values are escaped, known structures are converted into known LaTeX commands, required markers are injected exactly once, and pdfLaTeX compiles the result.

The run directory contains:

- CV JSON
- CV TeX
- CV PDF
- cover-letter JSON
- cover-letter TeX
- cover-letter PDF
- ZIP bundle

The final CV and cover-letter PDFs are uploaded to Baserow. Telegram receives the ZIP bundle.

**Reasoning:** generated content is untrusted data. Keeping document structure in reviewed templates and renderer code gives predictable output and prevents model text from becoming executable LaTeX structure.

### 11. Notification handoff

Notification work runs on its own queue. Telegram failures do not turn successful document generation into a failed application result.

**Reasoning:** Telegram is a delivery channel, not the business transaction. A temporary messaging outage should not trigger another expensive LLM generation cycle.

## Application boundaries and dependency direction

The project follows a lightweight ports-and-adapters approach.

`ports.py` defines protocols for:

- `JobRepository`
- `CompatibilityFilter`
- `Qualifier`
- `Tailor`
- `DocumentRenderer`
- `ArtifactPublisher`
- `Notifier`
- `DiscoveryProvider`

`ApplicationWorkflow` depends on these protocols, not on Baserow, Gemini, LiteLLM, Apify, or Telegram classes directly.

`Container` is the composition root that binds protocols to concrete implementations.

This matters for two reasons:

1. Core workflow tests can replace external services with fakes without changing application logic.
2. Replacing a provider should normally require an adapter change plus container wiring, not a rewrite of the workflow.

When adding a new external service, prefer this pattern:

```text
application need
   -> protocol
   -> integration adapter
   -> Container wiring
```

Avoid importing provider SDKs into `application/` or `domain/`.

## Tenant architecture

Tenant differences are deliberately narrow.

`config/users.toml` contains bootstrap information that must be known before Baserow can be queried:

- tenant key
- enabled flag
- renderer profile
- Baserow configuration table ID
- Baserow base URL
- tenant asset root
- environment-variable names for tenant Baserow and Fillout secrets

The larger tenant runtime configuration is stored in Baserow and validated into `TenantRuntimeConfig`.

Each tenant asset directory contains a master CV plus LaTeX templates.

Current renderer profiles are:

- `mojtaba`: fixed top-level CV sections with project and work-experience pipelines
- `mahsa`: dynamic section-oriented CV structure with a required education section and optional references

### Why configuration is split

The bootstrap registry is intentionally small and local because the application needs enough information to reach Baserow. Everything that operators may reasonably change without redeploying code, such as table IDs, search settings, thresholds, exclusions, actor IDs, and Telegram chat IDs, lives in Baserow.

This avoids putting every tenant setting into environment variables while still keeping secrets out of Baserow seed files and source control.

## Configuration model

There are four configuration layers.

### 1. Application environment

`Settings` loads `JOB_HUNT_*` variables from `.env` and defines infrastructure-wide values such as:

- Redis URL
- operator token
- request and compile timeouts
- Celery retry settings
- scheduler timezone
- LLM gateway routes
- artifact root
- shared Apify tokens
- Telegram bot credentials

### 2. LiteLLM provider registry

`config/llm-providers.json` describes upstream providers without putting secrets in source control.

A provider entry can specify:

- provider name and LiteLLM prefix
- numbered API-key environment prefix
- enabled state
- optional model-discovery endpoint
- explicit fast/balanced/powerful model assignments
- discovery allowlist
- model exclusions
- blocklist
- extra LiteLLM parameters

Example key naming:

```text
GROQ_API_KEY_1
GROQ_API_KEY_2
GEMINI_API_KEY_1
GEMINI_API_KEY_2
```

The config generator scans indexed keys. Adding more independent accounts does not require adding Python variables one by one.

### 3. Tenant bootstrap

`config/users.toml` points each tenant at its configuration table, asset root, renderer profile, and tenant-specific secret environment variables.

### 4. Tenant runtime data in Baserow

The Baserow Configuration table is parsed into `TenantRuntimeConfig`. Important values include:

- jobs, search-criteria, and prompts table IDs
- Baserow option IDs
- Fillout form and field IDs
- Apify actor IDs
- LinkedIn proxy/search settings
- discovery schedule interval
- company/title exclusions
- qualification threshold
- Telegram chat ID
- selection counts

The application fails configuration validation early when required table contracts or prompt contracts are missing.

## LLM architecture

### Capability groups instead of provider names

Application operations target capability groups, not concrete provider models. This is the main abstraction to preserve.

```text
application operation
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

This lets the system change providers, keys, and concrete models independently from prompt orchestration.

### Runtime config generation

`scripts/generate-litellm-config.py` reads `config/llm-providers.json` and the current environment to generate `config/litellm.runtime.yaml`.

For providers with discovery enabled, the generator queries the provider model catalog using configured keys. Models are filtered and classified into logical capability groups. Explicit assignments can override automatic classification.

Each model/account pair becomes a separate LiteLLM deployment. If a provider has three independent keys for one model, LiteLLM receives three deployments for that logical model group.

### Retry and failover ownership

LiteLLM is the first retry/failover layer for LLM requests.

The generated router configuration:

- calculates a default `num_retries` from the largest generated logical deployment pool
- defaults `allowed_fails` to `0`, so a failing deployment can be cooled immediately
- uses the configured cooldown interval
- defines capability fallback chains such as `job-powerful -> job-balanced -> job-fast`
- defines repair fallback from `repair-fast` to `repair-balanced` when both exist

`LITELLM_NUM_RETRIES`, `LITELLM_ALLOWED_FAILS`, `LITELLM_COOLDOWN_SECONDS`, and `LITELLM_ROUTING_STRATEGY` remain environment-level controls.

Celery retry is the outer safety net. It should run only after the LLM gateway has surfaced a retryable failure that it could not resolve internally.

**Reasoning:** deployment rotation belongs closest to the provider gateway. Celery retries an entire workflow task and is much more expensive than trying another healthy model/key deployment inside the same LLM request.

### Structured output contract

Prompts are stored in Baserow. An active prompt contains:

```text
Prompt Key
Version
Prompt Template
Output Structure
Temperature
Status
Enabled
```

Only rows with `Status = Active` and `Enabled = true` are loaded. Duplicate active prompt keys are rejected.

`Output Structure` is JSON Schema. The gateway is instructed to return JSON, and the application independently validates the response against the schema. If validation fails, the raw result can be sent to the repair capability group with the exact schema and validation error.

**Reasoning:** prompt text alone is not an API contract. Independent schema validation prevents malformed model output from silently flowing into renderers or persistence.

### Prompt ownership

The required prompt set differs by renderer profile.

Common prompts:

- `job_compatibility_filter`
- `job_page_content_extraction`
- `qualification_scoring`
- `cover_letter_generation`

Mojtaba additionally requires:

- `cv_project_selection`
- `cv_project_rewrite`
- `cv_work_experience_selection`
- `cv_work_experience_rewrite`
- `cv_skills_tailoring`
- `cv_summary_rewrite`

Mahsa additionally requires:

- `cv_work_experience_selection`
- `cv_work_experience_rewrite`
- `cv_skills_tailoring`
- `cv_summary_rewrite`
- `cv_references_inclusion`

Changing a prompt's schema is a code-contract change even though the row is stored outside the repository. Update tests and downstream assumptions when changing field shapes.

## Discovery and normalization

Scheduled discovery loads active Search Criteria rows from Baserow and turns them into LinkedIn search URLs. A row can either supply a pre-generated URL or fields used to construct one.

Discovery then:

1. calls the configured Apify search actor
2. normalizes provider records into `Job`
3. skips malformed provider records
4. applies company and title exclusion terms
5. deduplicates by stable identity
6. queues canonical submissions

The Apify adapter accepts a pool of independent tokens. Quota/capacity failures cool down the affected token and try another token. Redis-backed cooldown state makes exhaustion visible across worker processes.

This token pool remains application-wide rather than tenant-specific.

**Reasoning:** provider capacity is infrastructure capacity. Tying free-tier accounts to tenants would strand unused capacity and duplicate failover logic.

## Persistence and Baserow

Baserow has two roles:

1. user-facing business state
2. tenant-controlled configuration and prompt source

The Jobs table is expected to expose at least:

- Job ID
- Company Name
- Title
- Job Description
- Link
- Status
- Score
- Apply
- CV
- Cover Letter
- Date
- Location
- Contract Type

Live tenant loading validates this contract.

### Repository behavior

`BaserowJobRepository` finds jobs by external Job ID first and URL second. New rows start with the configured `new` status.

Qualification writes only score and Apply metadata. Status transitions are separate operations.

Artifact persistence writes the uploaded Baserow file objects after successful generation.

Existing working documents are not cleared before a new document generation succeeds.

**Reasoning:** regeneration should be transactional from the user's perspective. A failed replacement must not destroy the last known-good CV or cover letter.

## Redis state and run tracking

Redis is used intentionally for coordination state that is shared by workers but is not the long-term business system of record.

### `RunStore`

Stores:

- `RunStatus`
- replay request data
- notification metadata and stage timing

Concurrent run updates use Redis WATCH/MULTI transactions with retry, preventing one worker from blindly overwriting progress written by another worker.

### `RedisState`

Provides reusable primitives for:

- JSON objects with TTL
- snapshots
- checkpoints where active clients use them
- provider cooldowns
- shared counters
- checkpoint namespaces

Provider coordination state is global, while checkpoint namespaces can be isolated by run lineage.

### Discovery snapshots

Discovery can serialize tenant runtime configuration and active prompts once and let every child job refer to that snapshot.

**Reasoning:** all jobs from one discovery batch should evaluate against the same configuration/prompt version, and repeated Baserow reads should be avoided.

If a snapshot is unavailable by the time a child runs, the worker can fall back to current configuration rather than fail solely because an optimization expired.

## Celery and queue design

The queue split is based on workload characteristics, not arbitrary feature boundaries.

| Queue | Work |
| --- | --- |
| `fast` | discovery dispatch, discovery, normalization, persistence, compatibility, qualification |
| `documents` | tailoring, rendering, artifact upload |
| `notifications` | Telegram progress, final delivery, callback-related message work |

Default Compose concurrency is conservative:

```text
FAST_WORKER_CONCURRENCY=3
DOCUMENT_WORKER_CONCURRENCY=1
NOTIFICATION_WORKER_CONCURRENCY=1
```

`worker_prefetch_multiplier=1` is important because task durations are uneven. A worker should not reserve a large batch of long document jobs that another idle worker could process.

`task_acks_late=True` and `task_reject_on_worker_lost=True` favor recoverability if a worker dies while processing a task.

Global Celery time limits default to disabled because legitimate document jobs can be long-running. Individual provider and compiler operations have their own timeouts.

## Document generation

The worker Docker image is intentionally heavier than the API image because only document generation needs TeX packages.

Docker stages are split into:

- dependency base
- lightweight API/Beat image
- worker dependency image with TeX packages
- worker image

**Reasoning:** FastAPI, Beat, and other non-rendering processes should not carry the size and attack surface of a full LaTeX installation.

### Renderer strategies

The renderer interface is stable even though CV shapes differ.

Mojtaba uses named template markers for fixed sections. Mahsa renders a list of typed sections and requires exactly one education section.

Cover letters require exactly three non-empty paragraphs plus date and company name.

Template markers are injected exactly once. Missing or duplicated marker contracts are rendering errors rather than silently producing malformed documents.

## Telegram architecture

The application uses one shared Telegram bot and one shared webhook endpoint:

```text
POST /webhooks/telegram
```

`JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` authenticates Telegram requests.

Tenant routing is resolved from the incoming chat ID. The container loads tenant configuration until the matching `telegram_chat_id` is found, then caches that route in the API process.

Telegram actions can update Baserow status or request regeneration. A manual `Dropped` action also attempts to update the existing progress message to show that processing was stopped.

Notification state lives inside the run record but is deliberately separate from core generation success.

## Error handling and retries

Provider and workflow errors are classified through typed error kinds instead of being treated as one generic exception class.

Important categories include:

- validation
- business condition
- authentication
- rate limit
- transient provider failure
- malformed provider response
- configuration failure
- document rendering failure

Celery retry only applies to retryable workflow/provider failures. Retry delay uses:

1. provider supplied `retry_after` when available
2. configured rate-limit fallback for rate-limit errors
3. bounded exponential backoff for other transient failures

This distinction matters. A malformed configuration or invalid business input should fail visibly rather than consume repeated retries.

## Security boundaries

### Secrets

Provider and tenant secrets belong in `.env` or a deployment secret manager, not in committed configuration files.

`config/users.toml` stores environment-variable names for tenant secrets, not secret values.

### Operator API

Operator endpoints require `Authorization: Bearer <JOB_HUNT_OPERATOR_TOKEN>`.

### Fillout

Each tenant has its own Fillout bearer secret and expected form ID. A valid token for one tenant is not sufficient to submit to another tenant.

### Telegram

The shared Telegram webhook uses constant-time secret comparison.

### Generic URL ingestion

Generic URLs pass through the public-text fetch security boundary rather than being handed directly to an unrestricted HTTP client. Preserve SSRF protections when changing URL ingestion.

### LaTeX

Model-generated values are data, not trusted template code. Preserve escaping and structured renderer commands when adding new document fields.

## How to work with the codebase

### Start by locating the owning layer

Before changing code, classify the change:

| Change | Primary location |
| --- | --- |
| new job field or invariant | `domain/` plus normalization/persistence adapters |
| workflow order or gating | `application/` |
| new external provider implementation | `integrations/` |
| provider wiring | `container.py` |
| new HTTP endpoint/auth behavior | `api/` |
| new async stage or queue boundary | `worker.py` / `queueing.py` |
| new tenant assets | `tenants/<key>/` |
| new renderer structure | `rendering/` |
| application setting | `config.py` and `.env.example` |
| tenant-editable setting | Baserow Configuration contract/seed |
| LLM provider/model pool | `config/llm-providers.json` |
| LLM operation strength | operation-to-capability mapping |

### Keep orchestration provider-neutral

If `ApplicationWorkflow` needs a new capability, add or extend a protocol and inject it. Do not make application code instantiate an SDK client.

### Keep business state and transient state separate

Use Baserow for user-owned durable workflow data. Use Redis for execution coordination and TTL-bound runtime state.

Do not move Baserow status semantics into Redis just because Redis is faster.

### Treat retries as part of the architecture

Before adding a retry, decide what layer owns the failure:

- another API key/model deployment: LiteLLM or provider adapter
- transient whole-stage failure: Celery
- HTTP client transport retry: adapter only when it cannot duplicate business side effects
- invalid input/configuration: no retry

Blindly stacking retries at every layer can multiply calls and make failures take much longer to surface.

### Preserve idempotency

Any new discovery or processing path must assume:

- duplicate provider records exist
- tasks can be redelivered
- discovery windows can overlap
- retries can occur after partial success

Use stable job identity, repository checks, and stage-safe persistence rather than relying on queue delivery being exactly-once.

### Preserve cancellation checks around expensive work

If a new expensive stage is added after a job row exists, consider whether a manually dropped Baserow row should be checked before and after the stage.

### Prefer configuration over tenant forks

If two tenants differ only by thresholds, table IDs, actor IDs, prompt values, model strength, or search settings, keep the difference in configuration.

Add Python tenant-specific behavior only when the data contract or rendering strategy truly differs.

## Common extension workflows

### Add a new tenant using an existing renderer

1. Create `tenants/<key>/master_cv.json`.
2. Add `templates/cv_template.tex` and `templates/cover_letter_template.tex`.
3. Add `[users.<key>]` to `config/users.toml`.
4. Reuse `renderer = "mahsa"` or `renderer = "mojtaba"` if the data shape is compatible.
5. Create/import the tenant Baserow Configuration table.
6. Import the matching prompt seed and ensure only one active row exists for each required prompt key.
7. Add tenant Baserow and Fillout secrets to the deployment environment.
8. Set the tenant Telegram chat ID in Baserow.
9. Run local configuration validation.
10. Run live configuration validation.
11. Test a manual job, a threshold-gated job, and a discovery before enabling schedule-driven production use.

### Add a genuinely new renderer profile

1. Define the master-CV JSON contract.
2. Add a `CvRenderer` implementation in `rendering/profiles.py`.
3. Add a cover-letter renderer only if its structure is actually different.
4. Extend `TenantRegistry.get()` to select the profile.
5. Define required prompt keys for the new profile.
6. Update container prompt validation and tailoring strategy.
7. Add renderer unit tests for valid data, missing sections, escaping, and template marker errors.
8. Add a compile-level integration test when practical.

Do not copy the entire Mojtaba or Mahsa pipeline to create a third tenant unless the underlying workflow contract really differs.

### Add another LLM provider

Normally no application Python change is required.

1. Add a provider object to `config/llm-providers.json`.
2. Choose a unique indexed key prefix such as `OPENROUTER_API_KEY_`.
3. Configure explicit model groups or a discovery endpoint and allowlist.
4. Add `OPENROUTER_API_KEY_1`, `OPENROUTER_API_KEY_2`, etc. to the environment.
5. Generate the LiteLLM runtime config.
6. Run the LiteLLM smoke/live validation.
7. Add config-generation tests if provider behavior introduces a new registry feature.

The application should continue to request `job-fast`, `job-balanced`, `job-powerful`, and repair groups.

### Change model selection

Prefer changing capability-group membership or operation-to-group mapping instead of putting model IDs into application logic.

Model IDs are infrastructure decisions. Prompt operations express required capability.

### Add a new LLM operation

1. Create the Baserow prompt and JSON Schema.
2. Add the prompt key to the appropriate contract set if required for every run of that profile.
3. Add the application/tailoring method that renders the prompt values.
4. Decide its default capability group in `llm_routing.py` or configure an operation override.
5. Validate the output before downstream use.
6. Add tests for prompt rendering and output assumptions.

### Add a new submission type

1. Add a discriminated Pydantic submission model in `domain/models.py`.
2. Add it to `JobSubmission`.
3. Extend `SubmissionNormalizer.normalize()`.
4. Update Fillout mapping only if Fillout can submit that type.
5. Add API validation and normalization tests.
6. Keep the output as the same canonical `Job` model.

### Add a new Baserow field

Decide whether it is:

- required infrastructure contract
- optional business metadata
- configuration

Then update the appropriate Pydantic model/repository mapping and tests. If it is required on the Jobs table, add it to `REQUIRED_JOB_FIELDS` so live validation fails before production processing.

### Add a new Celery stage

Only create a new queue when its resource or failure characteristics differ materially from existing queues.

For a stage inside an existing workload class, prefer keeping it in the current task and exposing progress boundaries.

A separate queue is appropriate when you need independent concurrency, isolation, or service-level behavior.

## Testing strategy

The repository uses three broad test groups.

### Unit tests

`tests/unit/` covers domain rules, configuration parsing, API behavior, workflow decisions, discovery normalization, rendering, provider selection/config generation, run state, and worker behavior.

Unit tests should be the default location for new decision logic.

### Contract tests

`tests/contract/` tests adapter behavior and external-service assumptions with controlled HTTP/provider boundaries. Current coverage includes Apify, Baserow, Telegram, provider services, and the retained Gemini pool behavior.

Use contract tests when the important question is "does this adapter speak the expected external contract?" rather than "does this pure function return the right value?"

### Integration tests

Integration-marked tests exercise local infrastructure such as Redis or pdfLaTeX. They are intentionally separate from ordinary pure tests because they depend on installed services/tools.

Tests marked `live` can call configured external providers and should never be run casually with production credentials.

### Quality gates

CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Pytest enforces at least 70 percent coverage on the measured package set. `cli.py`, `container.py`, and `worker.py` are excluded from the coverage calculation because they are composition/runtime boundary modules, although they still have targeted tests where behavior warrants it.

Mypy runs in strict mode for the package, with narrow overrides for dynamic Celery/provider integration areas.

### Before opening a PR

Run at minimum:

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

If you changed provider routing, Docker, Compose, runtime config generation, or document rendering, also run the relevant smoke/integration path rather than relying on unit tests alone.

## Local development

### Requirements

- Python 3.12+
- `uv`
- Docker Engine / Docker Compose
- provider and tenant credentials for live operation
- local TeX only if rendering outside the worker container

### Install

```bash
uv sync --extra dev
cp .env.example .env
```

Populate `.env` with the required secrets.

### Validate static tenant assets

```bash
uv run job-hunt config validate
```

This verifies tenant registry entries and required local tenant files.

### Validate live configuration

With Baserow and LiteLLM available:

```bash
uv run job-hunt config validate --live
```

This loads live tenant configuration, validates Baserow table/prompt contracts, and verifies configured LiteLLM capability groups are exposed by the gateway.

### Generate LiteLLM runtime configuration

For local gateway work, use the same generator used by deployment:

```bash
python scripts/generate-litellm-config.py
```

The generated file is `config/litellm.runtime.yaml` unless configured otherwise.

### Start the stack

```bash
docker compose up --build -d
```

Inspect:

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker-fast
docker compose logs -f worker-documents
docker compose logs -f litellm
```

FastAPI docs are available at:

```text
http://127.0.0.1:8000/docs
```

Flower is bound locally at:

```text
http://127.0.0.1:5555
```

### CLI examples

Submit a job:

```bash
uv run job-hunt submit mahsa --input examples/job.json
```

Force a manual regeneration:

```bash
uv run job-hunt submit mahsa --input examples/job.json --force
```

Run discovery:

```bash
uv run job-hunt discover mojtaba
```

Inspect a run:

```bash
uv run job-hunt status RUN_UUID
```

Retry using stored replay data:

```bash
uv run job-hunt retry RUN_UUID
```

Render already-tailored JSON locally:

```bash
uv run job-hunt render mahsa --input tailored.json --output ./render-test
```

### API example

```bash
curl -X POST http://127.0.0.1:8000/v1/tenants/mahsa/jobs \
  -H "Authorization: Bearer $JOB_HUNT_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entry_type":"linkedin","linkedin_job_id":123456}'
```

The response contains a run UUID. Query `/v1/runs/{uuid}` rather than expecting the application files in the initial HTTP response.

## Production deployment

The supported normal VPS deployment path is:

```bash
bash scripts/deploy-vps.sh
```

Do not replace this with an ad hoc sequence of `docker compose down`, `build`, and `up` unless you are intentionally debugging the deployment script.

The deployment script performs the repository-specific sequence:

1. `git pull --ff-only`
2. build the API image
3. generate a fresh LiteLLM runtime configuration from current provider keys and registry
4. atomically replace the runtime config on the host
5. build the worker image
6. validate Mahsa LaTeX compatibility
7. start Redis
8. force-recreate LiteLLM so the new deployment pools are loaded
9. wait for LiteLLM liveliness
10. run live application configuration validation
11. force-recreate API, workers, Beat, and Flower
12. show Compose state
13. verify API liveness and readiness
14. verify Redis responds to `PING`

**Reasoning:** model/provider pools are generated from the live VPS environment. Restarting containers without regenerating and reloading the LiteLLM config can leave the running gateway out of sync with configured keys.

The tracked nginx configuration is under `deploy/nginx/job-hunt-automation.conf`. VPS-specific `.env`, DNS, TLS certificates, and host installation state must not be committed.

Run VPS tests with:

```bash
bash scripts/test-vps.sh
```

The Compose test service mounts the current source and tests so it does not accidentally execute stale test files baked into an older image.

## Operational debugging

### Check overall state

```bash
docker compose ps
```

### Check API

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

`live` only proves the process is serving. `ready` also verifies Redis connectivity.

### Check LiteLLM

```bash
docker compose logs --tail=200 litellm
```

The deployment script also checks LiteLLM's internal liveliness endpoint from inside the container.

### Check workers separately

```bash
docker compose logs --tail=200 worker-fast
docker compose logs --tail=200 worker-documents
docker compose logs --tail=200 worker-notifications
```

Do not debug a document queue backlog by looking only at `worker-fast`.

### Check a specific run

```bash
uv run job-hunt status RUN_UUID
```

The run record exposes the current state/stage, task ID, error metadata, and notification progress.

### Typical failure ownership

| Symptom | First place to inspect |
| --- | --- |
| provider/model 429 | LiteLLM logs and generated deployment pool |
| all Apify accounts exhausted | fast worker and Redis cooldown state |
| malformed structured LLM output | prompt schema, LLM logs, repair route |
| missing Baserow fields | live config validation |
| job keeps getting skipped as duplicate | stable identity and existing Baserow row |
| dropped job still spending work | cancellation checks around the new stage |
| TeX/PDF failure | document worker, generated JSON/TEX, template markers |
| Telegram failure after PDFs exist | notification worker only |
| API ready fails | Redis availability |
| new key/model not used after deploy | regenerated `litellm.runtime.yaml` and LiteLLM recreation |

## Design invariants

These are not incidental implementation details. Treat them as architecture constraints unless there is a deliberate migration plan.

1. **One shared application, narrow tenant differences.** Do not recreate separate end-to-end workflows per user.
2. **Application logic depends on ports, not provider SDKs.** External services stay behind adapters.
3. **Canonical `Job` is the boundary after normalization.** Downstream stages should not branch on every ingress format.
4. **Automatic processing is idempotent.** Duplicate discovery should not consume qualification/document capacity.
5. **Baserow status is user-owned.** Manual cancellation must be respected during long workflows.
6. **Numeric qualification threshold gates document generation.** `should_apply` is persisted metadata, not the gate itself.
7. **LLM code asks for capability groups, not concrete provider keys.** Provider/model deployment belongs in LiteLLM configuration.
8. **LiteLLM handles immediate deployment failover.** Celery is the outer task-level safety net.
9. **Structured LLM output is independently schema validated.** Never trust model formatting alone.
10. **Only dependency-independent tailoring calls run in parallel.** Preserve data dependencies between selection/rewrite/summary/cover-letter stages.
11. **Generated content does not control LaTeX structure.** Render known JSON shapes into reviewed commands/templates.
12. **Notification failure is not generation failure.** Keep Telegram isolated from document-worker success.
13. **Previous good artifacts survive failed regeneration.** Do not clear working attachments before replacements are safely stored.
14. **Queue separation reflects workload isolation.** Long document tasks must not starve discovery and qualification.
15. **Provider capacity is shared infrastructure.** Do not reintroduce per-tenant LLM or Apify key pools without a strong reason.
16. **Deployment regenerates the LiteLLM runtime config.** Provider registry plus VPS secrets are the source of deployment pools.
17. **Secrets stay outside committed config.** Source files may contain secret aliases, never live tokens.

## Legacy and migration code

The repository still contains modules from the earlier direct-Gemini routing architecture, including `gemini_pool.py`, `gemini_catalog.py`, and related compatibility helpers/tests.

The active production composition path is defined by `Container`, which currently constructs the structured LLM client through `build_routed_structured_client()` in `integrations/llm_routing.py`. That client talks to LiteLLM.

When changing current production LLM behavior, follow the active path first:

```text
container.py
  -> integrations/llm_routing.py
  -> integrations/litellm_config.py
  -> config/llm-providers.json
  -> LiteLLM Proxy
```

Do not add new production behavior to a retained direct-Gemini pool merely because a similarly named test or older documentation exists. First confirm the module is actually wired by the composition root.

Retained code can still be useful for migration reference and tests, but architecture documentation should describe what `Container`, `worker.py`, and Docker Compose actually instantiate today.

## Additional documentation

- `docs/operations.md`: run lifecycle and troubleshooting details
- `docs/queue-architecture.md`: queue isolation rationale
- `docs/tenant-onboarding.md`: tenant setup checklist
- `docs/vps-deployment.md`: VPS-specific deployment notes

When these documents disagree with current wiring, treat executable configuration and the composition root as the source of truth, then update the stale documentation as part of the same change.
