# Tenant onboarding

1. Choose an existing renderer profile (`mahsa` dynamic sections or `mojtaba` fixed sections). Add a new renderer only if the JSON/injection contract is genuinely different.
2. Add `tenants/<key>/master_cv.json`, `templates/cv_template.tex`, and `templates/cover_letter_template.tex`. Markers must match the chosen renderer exactly.
3. Add a `[users.<key>]` entry to `config/users.toml`. Store only environment-variable aliases, never values.
4. Copy the closest seed CSV, change tenant/table/form/option/chat settings, import it into a Baserow Configuration table, and set `config_table_id`.
5. Import the matching `config/seeds/<tenant>-prompts.csv` into the configured Prompts table. Keep one enabled `qualification` row; all other enabled prompt rows are composed into the tenant-specific tailoring instruction set.
6. Set provider credential variables, then run local and live configuration validation.
7. Configure Fillout to call `/webhooks/fillout/<key>` with `Authorization: Bearer <tenant secret>` and the expected form ID.
8. Exercise one forced operator submission, one normal gated submission, and one discovery in a non-production destination before enabling the tenant.

Required Baserow Jobs fields are validated at startup: Job ID, Company Name, Title, Job Description, Link, Status, Score, Apply, CV, and Cover Letter. Additional metadata fields are supported by the repository mapping.
