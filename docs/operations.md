# Operations and troubleshooting

## Run lifecycle

API calls return HTTP 202 and a UUID. Query `/v1/runs/{uuid}` or use `job-hunt status`. States are `queued`, `running`, `succeeded`, `failed`, or `skipped`; `stage` identifies the last boundary reached. Failed jobs do not stop other jobs in a discovery batch.

Development logs are readable console events. Non-development environments emit JSON with timestamp, level, event, tenant, run/task/job identifiers, stage, attempt/provider/status where available, duration, and exception context. Secret-like fields and oversized strings are redacted.

## Common failures

| Error/stage | Likely cause | Check |
|---|---|---|
| `configuration_error` | Missing config row, prompt, field, option, or environment secret | `job-hunt config validate TENANT --live`, Configuration table, `users.toml`, container environment |
| `authentication` | Expired/wrong provider credential | Corresponding `*_TOKEN`, `*_API_KEY`, or `*_URL` variable |
| `rate_limit` | Provider quota or concurrency limit | `Retry-After`, Apify/Gemini/Telegram dashboard, configured item/concurrency limits |
| `malformed_provider_response` | Provider or LLM output no longer satisfies the typed contract | Full exception and prompt schema; retry after provider recovery |
| `document_rendering` | Invalid tailored JSON, template marker, or LaTeX | Run directory `.json`/`.tex`, renderer profile, `pdflatex` tail in the exception |
| URL validation | Non-public host, redirect, oversized/non-text response | Use a public HTTP(S) posting URL; private/reserved networks are intentionally blocked |
| Redis readiness | Redis unavailable | `docker compose ps`, `docker compose logs redis` |

Transient network, 429, and 5xx failures retry with bounded exponential backoff and jitter. Validation, authentication, configuration, and permanent 4xx failures do not retry. Unexpected exceptions retain stack traces in logs. Failure alerts are intentionally not sent to Telegram.

## Safe recovery

- Retry a failed run through the API or CLI; replay inputs expire with the run TTL.
- A tenant/job lock prevents simultaneous processing of the same identity.
- Reprocessing intentionally clears CV, cover letter, score, and apply fields and resets Status to New before rescoring.
- Cloudinary paths are deterministic and use overwrite; Baserow lookups are filtered; Telegram is called only after persistence succeeds.
- Do not run tests marked `live` without explicitly configured non-production credentials.

