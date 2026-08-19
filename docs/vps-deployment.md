# VPS deployment guide

This repository does not connect to or modify the VPS. Perform deployment manually after local acceptance.

1. Install Docker Engine and Compose on the VPS and copy/clone this repository.
2. Create `.env` from `.env.example`, use long independent operator/webhook secrets, and restrict file permissions.
3. Update `config/users.toml` with real Configuration table IDs. Keep all service tokens only in `.env` or the VPS secret manager.
4. Run `docker compose build`, `docker compose run --rm api job-hunt config validate --live`, then `docker compose up -d`.
5. Put TLS/reverse proxying in front of port 8000. Expose only HTTPS; Redis must remain private.
6. Set Fillout webhook URLs and bearer headers after `/health/ready` succeeds.
7. Configure Docker log rotation and back up Redis plus the artifact volume. Baserow and Cloudinary require their own provider-side backup/retention policy.
8. Watch JSON logs and run status during the first scheduled and webhook submissions. Scale workers independently; keep `worker_prefetch_multiplier=1` for fair long-running document jobs.

Upgrade with a tested image, `docker compose up -d --build`, and a readiness check. Roll back by restoring the previous image/tag and restarting the services; no n8n actions are part of this repository.

