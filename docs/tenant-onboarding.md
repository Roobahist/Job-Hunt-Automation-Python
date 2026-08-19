# Tenant onboarding

1. Choose an existing renderer profile (`mahsa` dynamic sections or `mojtaba` fixed sections). Add a new renderer only if the JSON/injection contract is genuinely different.
2. Add `tenants/<key>/master_cv.json`, `templates/cv_template.tex`, and `templates/cover_letter_template.tex`. Markers must match the chosen renderer exactly.
3. Add a `[users.<key>]` entry to `config/users.toml`. Store only environment-variable aliases, never values.
4. Copy the closest configuration seed CSV, change tenant/table/form/option/chat settings, import it into a Baserow Configuration table, and set `config_table_id`.
5. Import the matching prompt seed into the configured Prompts table. Prompt rows use the columns `Prompt Key`, `Version`, `Prompt Template`, `Output Structure`, `Temperature`, `Status`, and `Enabled`.
6. Keep at most one row per Prompt Key with `Status = Active` and `Enabled = true`. Draft and disabled versions are ignored. The active row's JSON Schema is used directly for Gemini structured output and for post-response validation.
7. Mojtaba requires `cv_project_selection`, `cv_project_rewrite`, `cv_work_experience_selection`, `cv_work_experience_rewrite`, `cv_skills_tailoring`, `cv_summary_rewrite`, `cover_letter_generation`, `job_page_content_extraction`, and `qualification_scoring`.
8. Mahsa uses the same prompt contract plus `cv_references_inclusion`. Her CV assembly is section-based: work selection/rewrite, skills, summary, and references inclusion operate on the `entries`, `label_rows`, `text`, and `references` sections. The project prompt definitions remain available in her prompt table but are not part of her current CV assembly flow.
9. Both profiles use the same Gemini structured-output client, schema validation, auto-fix behavior, qualification flow, page-content extraction, and document-generation workflow. Profile-specific code is limited to how the master CV is read and rebuilt.
10. Set provider credential variables, then run local and live configuration validation.
11. Configure Fillout to call `/webhooks/fillout/<key>` with `Authorization: Bearer <tenant secret>` and the expected form ID.
12. Exercise one forced operator submission, one normal gated submission, and one discovery in a non-production destination before enabling the tenant.

If an LLM result violates the active prompt's `Output Structure`, the workflow sends the malformed result, validation error, and required JSON Schema to an auto-fix Gemini call. The repaired result must pass the same JSON Schema before the workflow continues.

Required Baserow Jobs fields are validated at startup: Job ID, Company Name, Title, Job Description, Link, Status, Score, Apply, CV, and Cover Letter. Additional metadata fields are supported by the repository mapping.
