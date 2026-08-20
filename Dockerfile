FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS deps

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM deps AS app-base
COPY . .
RUN uv sync --no-dev && mkdir -p /app/runs && chown -R app:app /app

FROM app-base AS api
USER app
CMD ["uvicorn", "job_hunt.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM app-base AS beat
USER app
CMD ["celery", "-A", "job_hunt.worker.celery_app", "beat", "--loglevel=INFO"]

FROM deps AS worker-deps
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends texlive-latex-base texlive-latex-extra texlive-fonts-recommended texlive-fonts-extra \
    && rm -rf /var/lib/apt/lists/*

FROM worker-deps AS worker
USER root
COPY . .
RUN uv sync --no-dev && mkdir -p /app/runs && chown -R app:app /app
USER app
CMD ["celery", "-A", "job_hunt.worker.celery_app", "worker", "--loglevel=INFO"]
