# Architecture and maintenance guide

This document describes the production architecture that is actually instantiated by the repository and records the main maintenance rules discovered during the codebase audit.

## Runtime ownership

The active LLM path is:

```text
Container
  -> integrations/llm_routing.py
  -> LiteLLM Proxy
  -> config/llm-providers.json
  -> provider deployments
```

Application code requests logical capability groups. Provider API keys, deployment selection, immediate retries, cooldowns, and inter-group fallbacks belong to LiteLLM.

Current groups:

| Group | Deployments | Main use |
| --- | --- | --- |
| `job-fast` | Gemini 3.5 Flash Lite | compatibility, page extraction |
| `job-balanced` | Gemini 3.5 Flash, Mistral Medium | qualification, selection, skills |
| `job-powerful` | Mistral Large | rewriting, summary, cover letter |
| `repair-fast` | fast generation deployments plus Mistral Small | first repair tier |
| `repair-balanced` | balanced generation deployments plus Mistral Small | stronger repair tier |

Current provider limits encoded in the registry are 15 RPM / 250k TPM for Gemini 3.5 Flash Lite, 5 RPM / 250k TPM for Gemini 3.5 Flash, 50 RPM / 25k TPM for Mistral Medium, 4 RPM / 250k TPM for Mistral Large, and 50 RPM / 50k TPM for Mistral Small. Gemini account observations also show 500 RPD for Flash Lite and 20 RPD for Flash. Provider responses remain authoritative if account limits change.

Groq is not part of the active registry. The old direct Gemini pool and direct provider catalog validation modules were removed because they duplicated ownership that now belongs to LiteLLM.

## Retry hierarchy

One LLM call should exhaust immediate capacity before a whole Celery task is delayed:

```text
logical group
  -> deployment/key A
  -> deployment/key B
  -> other deployments in group
  -> configured fallback group
  -> deployments in fallback group
  -> gateway error only after immediate options fail
  -> Celery outer retry
```

Generation fallbacks are `job-powerful -> job-balanced -> job-fast`. Repair uses `repair-fast -> repair-balanced`; `repair-balanced` already has cross-provider redundancy.

Celery defaults to 8 task retries. Rate-limit errors use a 65 second fallback delay unless a classified adapter supplies `retry_after`. Transient delays use exponential backoff from 5 seconds and cap at 300 seconds.

Apify uses the same principle at the adapter boundary. A capacity error cools that token and tries other currently available tokens. If every token is already cooling down, the adapter raises a retryable rate-limit error with the shortest known remaining cooldown instead of probing a token known to be unavailable.

## Structured output and checkpoints

Every active prompt has a Baserow `Output Structure` JSON Schema. The response is parsed and independently validated. Invalid output is sent through a repair group and validated again against the same schema.

Validated LLM results are checkpointed in Redis. The digest includes logical model group, prompt key/version, rendered prompt, and schema. Normal task retries keep the same checkpoint namespace and can reuse completed work. Fresh regeneration creates a new namespace.

## Data ownership

Baserow is durable business state. Redis is runtime coordination.

Baserow owns job source metadata, score/apply values, user status, and final artifacts. Updates are deliberately narrow. Forced reprocessing refreshes source job metadata but does not clear existing qualification or document fields before replacement work succeeds.

Redis owns run/replay data, discovery snapshots, locks, progress metadata, provider cooldowns, and LLM checkpoints.

## Security boundaries

Generic URL intake must pass through `security.fetch_public_text`. The URL boundary rejects non-HTTP schemes, private/reserved resolved addresses, credential-bearing URLs, oversized responses, unsupported content types, and unsafe redirect destinations.

LinkedIn detection requires a real `linkedin.com` host or subdomain, not a hostname that merely ends with the same text.

Renderer code owns LaTeX structure. Ordinary values are escaped, known markers are injected once, and pdflatex runs with `-no-shell-escape`.

## Code ownership

| Concern | Primary location |
| --- | --- |
| domain invariant | `domain/` |
| workflow sequencing/gating | `application/` |
| provider adapter | `integrations/` |
| dependency wiring | `container.py` |
| HTTP/webhooks | `api/` |
| async task boundary | `worker.py`, `queueing.py` |
| rendering | `rendering/` |
| tenant assets | `tenants/<key>/` |
| LLM pool | `config/llm-providers.json` |
| app setting | `config.py`, `.env.example` |
| tenant runtime setting | Baserow Configuration table |

Provider-neutral application code should depend on the protocols in `ports.py`, especially `StructuredClient` and `JobExtractor`, rather than concrete provider clients.

## Queue design

The `fast` queue handles discovery, normalization, persistence, compatibility, and qualification. The `documents` queue handles tailoring, rendering, and artifact upload. The `notifications` queue handles Telegram. This isolates long document work from job intake and isolates notification failures from successful generation.

## Known refactor debt

### `worker.py`

`worker.py` currently owns Celery configuration, retry planning, progress state, discovery, submission, document, and notification tasks. It is the largest structural refactor target and is currently exempted from Ruff formatting and coverage.

A behavior-preserving split should move Celery configuration, progress helpers, retry planning, and each task family into smaller modules. Do this separately from provider changes so task semantics remain easy to verify.

### Historical LLM module names

The active path is multi-provider, but workflow orchestration still lives in historically named `gemini.py`, `gemini_parallel.py`, and `gemini_mahsa.py`. Direct pool/catalog code is gone. Renaming these remaining modules should be a mechanical refactor with imports/tests moved together.

### URL identity query stripping

`domain/identity.py` removes query parameters when canonicalizing URLs. This is useful against tracking parameters but can collapse distinct postings on sites where job identity exists only in a query value. Existing stable identities depend on current behavior, so changing it requires an explicit migration plan.

### LiteLLM image pinning

Compose uses `ghcr.io/berriai/litellm:main-stable`. Pin a tested version or digest in a dedicated deployment change so an upstream image change cannot silently alter routing behavior during an unrelated deploy.

### Historical dependency set

Some provider libraries remain in the dependency/lock set while the historical direct structured-client compatibility code still exists. Remove those only in the same change that finishes the LLM module rename/direct-client cleanup and regenerate `uv.lock` at the same time.

## Change rules

Preserve these invariants unless a migration explicitly changes them:

1. One shared application with narrow tenant differences.
2. Canonical `Job` is the boundary after normalization.
3. Application logic does not select provider keys.
4. Baserow is durable state; Redis is runtime coordination.
5. Automatic processing is idempotent.
6. Baserow status is user-owned.
7. Numeric qualification threshold gates document generation.
8. LiteLLM owns immediate LLM failover; Celery is the outer retry layer.
9. Structured output and repaired output are independently validated.
10. Normal retries reuse checkpoints; fresh regeneration uses a new namespace.
11. Only dependency-independent LLM calls run concurrently.
12. Renderer code owns document structure.
13. Notification failure is not generation failure.
14. Last known-good documents survive failed regeneration.
15. Only the document worker carries TeX.
