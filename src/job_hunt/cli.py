from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import TypeAdapter
from redis import Redis

from job_hunt.config import Settings, load_registry, read_seed
from job_hunt.container import Container
from job_hunt.domain.models import JobSubmission, RunStatus, TailoredContent
from job_hunt.run_store import RunStore
from job_hunt.worker import discover_tenant, process_submission

app = typer.Typer(no_args_is_help=True, help="Operate the multi-tenant job-hunt workflow.")
config_app = typer.Typer(help="Validate local and Baserow configuration.")
app.add_typer(config_app, name="config")


def _settings() -> Settings:
    return Settings()


def _store(settings: Settings) -> RunStore:
    return RunStore(
        Redis.from_url(settings.redis_url, decode_responses=True), settings.run_ttl_seconds
    )


@app.command()
def submit(
    tenant: str,
    input_file: Annotated[Path, typer.Option("--input", exists=True, readable=True)],
    force: bool = False,
) -> None:
    settings = _settings()
    payload = json.loads(input_file.read_text(encoding="utf-8"))
    submission: Any = TypeAdapter(JobSubmission).validate_python(payload)
    run = RunStatus(tenant=tenant, kind="manual")
    store = _store(settings)
    store.save(run)
    dumped = TypeAdapter(JobSubmission).dump_python(submission, mode="json")
    store.save_request(
        run.run_id, {"tenant": tenant, "payload": dumped, "kind": "manual", "force": force}
    )
    task = process_submission.delay(tenant, dumped, str(run.run_id), force)
    store.update(run.run_id, task_id=str(task.id))
    typer.echo(str(run.run_id))


@app.command()
def discover(tenant: str) -> None:
    settings = _settings()
    run = RunStatus(tenant=tenant, kind="discovery")
    store = _store(settings)
    store.save(run)
    store.save_request(run.run_id, {"tenant": tenant, "kind": "discovery"})
    task = discover_tenant.delay(tenant, str(run.run_id))
    store.update(run.run_id, task_id=str(task.id))
    typer.echo(str(run.run_id))


@app.command()
def status(run_id: UUID) -> None:
    found = _store(_settings()).get(run_id)
    if not found:
        raise typer.BadParameter("Run not found")
    typer.echo(found.model_dump_json(indent=2))


@app.command()
def retry(run_id: UUID) -> None:
    settings = _settings()
    store = _store(settings)
    original = store.get(run_id)
    request = store.get_request(run_id)
    if not original or not request:
        raise typer.BadParameter("Run or replay data not found")
    run = RunStatus(tenant=original.tenant, kind=original.kind, original_run_id=run_id)
    store.save(run)
    store.save_request(run.run_id, request)
    if request["kind"] == "discovery":
        task = discover_tenant.delay(original.tenant, str(run.run_id))
    else:
        task = process_submission.delay(
            original.tenant, request["payload"], str(run.run_id), bool(request.get("force", False))
        )
    store.update(run.run_id, task_id=str(task.id))
    typer.echo(str(run.run_id))


@app.command()
def render(
    tenant: str,
    input_file: Annotated[Path, typer.Option("--input", exists=True)],
    output_directory: Annotated[Path, typer.Option("--output")],
    basename: str = "application",
) -> None:
    context = Container(_settings()).registry.get(tenant)
    content = TailoredContent.model_validate_json(input_file.read_text(encoding="utf-8"))
    result = context.renderer.render(content, output_directory, basename)
    typer.echo(result.model_dump_json(indent=2))


@config_app.command("validate")
def validate_configuration(tenant: str | None = None, live: bool = False) -> None:
    settings = _settings()
    registry = load_registry(settings.registry_path)
    selected = [tenant] if tenant else list(registry)
    failures: list[str] = []
    for key in selected:
        try:
            bootstrap = registry[key]
            root = Path(bootstrap.tenant_root)
            if not (root / "master_cv.json").is_file():
                raise ValueError("master_cv.json is missing")
            if not (root / "templates/cv_template.tex").is_file():
                raise ValueError("CV template is missing")
            if live:
                Container(settings).tenant(key)
            typer.echo(f"OK {key}")
        except Exception as exc:
            failures.append(f"{key}: {exc}")
    if failures:
        typer.echo("\n".join(failures), err=True)
        raise typer.Exit(1)


@config_app.command("seed")
def validate_seed(path: Annotated[Path, typer.Argument(exists=True)]) -> None:
    typer.echo(read_seed(path).model_dump_json(indent=2))


if __name__ == "__main__":
    app()
