# Tenant onboarding

1. Choose an existing renderer profile (`mahsa` dynamic sections or `mojtaba` fixed sections). Add a new renderer only when the master-CV or LaTeX injection contract is genuinely different.
2. Add `tenants/<key>/master_cv.json`, `templates/cv_template.tex`, and `templates/cover_letter_template.tex`.
3. Add `[users.<key>]` to `config/users.toml`. Tenant-specific secret aliases are Baserow and Fillout. Apify, Gemini, and Telegram bot credentials are application-wide.
4. Copy the closest configuration seed CSV, change tenant/table/form/option/chat settings, import it into a Baserow Configuration table, and set `config_table_id`.
5. Import the matching prompt seed into the configured Prompts table. Prompt rows use `Prompt Key`, `Version`, `Prompt Template`, `Output Structure`, `Temperature`, `Status`, and `Enabled`.
6. Keep at most one row per Prompt Key with `Status = Active` and `Enabled = true`. Draft and disabled versions are ignored.
7. Mojtaba requires `cv_project_selection`, `cv_project_rewrite`, `cv_work_experience_selection`, `cv_work_experience_rewrite`, `cv_skills_tailoring`, `cv_summary_rewrite`, `cover_letter_generation`, `job_page_content_extraction`, and `qualification_scoring`.
8. Mahsa requires `cv_work_experience_selection`, `cv_work_experience_rewrite`, `cv_skills_tailoring`, `cv_summary_rewrite`, `cv_references_inclusion`, `cover_letter_generation`, `job_page_content_extraction`, and `qualification_scoring`. Her section-based pipeline treats the relevant CV entries through the work-experience abstraction and does not require project selection or project rewrite prompts.
9. Configure the application-wide pools with `JOB_HUNT_APIFY_TOKENS` and `JOB_HUNT_GEMINI_API_KEYS`. Each key/token should represent a separate provider account.
10. Configure one shared Telegram bot with `JOB_HUNT_TELEGRAM_BOT_TOKEN` and one shared webhook secret with `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET`. Each tenant keeps its own `telegram_chat_id` in Baserow. Callback routing is resolved from Telegram's incoming `message.chat.id`.
11. Configure model order, repair order, proactive quota limits, and optional tailoring parallelism only when the defaults do not match current provider limits.
12. Run `uv run job-hunt config validate`. After credentials are ready, run `uv run job-hunt config validate --live` to validate Baserow and confirm every configured Gemini model ID is available.
13. Configure Fillout to call `/webhooks/fillout/<key>` with `Authorization: Bearer <tenant fillout secret>`.
14. Register the shared Telegram bot webhook at `/webhooks/telegram` using `JOB_HUNT_TELEGRAM_WEBHOOK_SECRET` as Telegram's secret token.
15. Exercise one forced operator submission, one normal score-gated submission, and one scheduled discovery in a non-production destination before enabling regular discovery.

The active Baserow `Output Structure` is both the Gemini response schema and the independent post-response validator. Invalid output is repaired by the configured repair tier and validated again.

Discovery batches snapshot the active configuration and prompts once. All jobs produced by that discovery share the same snapshot and prompt versions. Manual and Fillout jobs continue to read current Baserow configuration.

Generated PDFs upload directly to Baserow. Reprocessing preserves previous working documents until their replacements are successfully persisted. No Cloudinary configuration is required.

Required Jobs-table fields are validated when live tenant configuration is loaded: Job ID, Company Name, Title, Job Description, Link, Status, Score, Apply, CV, Cover Letter, Date, Location, and Contract Type.
