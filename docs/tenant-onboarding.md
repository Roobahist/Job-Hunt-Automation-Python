# Tenant onboarding

1. Choose an existing renderer profile: `mahsa` or `mojtaba`. Add a renderer only when the master-CV/LaTeX contract genuinely differs.
2. Add `tenants/<key>/master_cv.json`, `templates/cv_template.tex`, and `templates/cover_letter_template.tex`.
3. Add `[users.<key>]` to `config/users.toml`. Tenant-specific secret aliases are Baserow and Fillout only. Apify, LiteLLM provider keys, and the Telegram bot are application-wide.
4. Copy the closest configuration seed CSV, change tenant/table/form/option/chat settings, import it into a Baserow Configuration table, and set `config_table_id` in `config/users.toml`.
5. Import the matching prompt seed into the configured Prompts table. Prompt rows use `Prompt Key`, `Version`, `Prompt Template`, `Output Structure`, `Temperature`, `Status`, and `Enabled`.
6. Keep at most one row per Prompt Key with `Status = Active` and `Enabled = true`.
7. Mojtaba requires project selection/rewrite, work-experience selection/rewrite, skills, summary, cover letter, page extraction, qualification, and compatibility prompts.
8. Mahsa requires work-experience selection/rewrite, skills, references inclusion, summary, cover letter, page extraction, qualification, and compatibility prompts. Her section-based CV path does not use the Mojtaba project prompts.
9. Configure application-wide Apify tokens with `JOB_HUNT_APIFY_TOKENS`.
10. Configure provider API keys as numbered environment variables such as `GEMINI_API_KEY_1` and `MISTRAL_API_KEY_1`. Provider/model group assignments live in `config/llm-providers.json`, not in tenant Baserow rows.
11. Configure one shared Telegram bot with `JOB_HUNT_TELEGRAM_BOT_TOKEN` and one shared webhook secret with `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET`. Each tenant keeps its own `telegram_chat_id` in Baserow.
12. Run `uv run job-hunt config validate`. After Baserow/LiteLLM are reachable, run `uv run job-hunt config validate --live` to validate tenant contracts and logical LiteLLM groups.
13. Configure Fillout to call `/webhooks/fillout/<key>` with `Authorization: Bearer <tenant fillout secret>`.
14. Register the shared Telegram bot webhook at `/webhooks/telegram` using `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` as Telegram's secret token.
15. Exercise one forced submission, one score-gated submission, and one scheduled discovery in a safe destination before enabling regular discovery.

Historical configuration rows such as `gemini_model` are ignored by the current runtime. LLM routing is intentionally application-wide so tenants share the same independent provider-account pool.

The Baserow `Output Structure` is the response contract and independent JSON Schema validator. Invalid output is repaired through the configured repair capability group and validated again.

Discovery batches snapshot active runtime configuration/prompts once. All jobs in that batch share the same snapshot. Manual/Fillout jobs read current Baserow configuration.

Generated PDFs and the application ZIP upload directly to Baserow. Forced reprocessing refreshes source job fields while last known-good documents remain until replacements are successfully persisted.

Required Jobs-table fields are validated when live tenant configuration is loaded: Job ID, Company Name, Title, Job Description, Link, Status, Score, Apply, CV, Cover Letter, Date, Location, and Contract Type.
