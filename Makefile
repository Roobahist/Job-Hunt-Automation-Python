.PHONY: check test lint typecheck

check: lint typecheck test

lint:
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

