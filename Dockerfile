FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project
COPY . .
RUN uv sync --no-dev && mkdir -p /app/runs && chown -R app:app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM base AS api
USER app
CMD ["uvicorn", "job_hunt.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS beat
USER app
CMD ["celery", "-A", "job_hunt.worker.celery_app", "beat", "--loglevel=INFO"]

FROM base AS worker
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends texlive-latex-base texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra \
    && rm -rf /var/lib/apt/lists/*
USER app
CMD ["celery", "-A", "job_hunt.worker.celery_app", "worker", "--loglevel=INFO"]
