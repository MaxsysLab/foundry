# Foundry: developer tasks. Assumes `uv` is on PATH.
.PHONY: help dev test cov lint fmt typecheck clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

dev: ## Install with dev deps
	uv sync --extra dev

test: ## Run the test suite (offline; scripted provider + fake worker)
	uv run pytest -q

cov: ## Tests with coverage
	uv run pytest --cov=foundry --cov-report=term-missing

lint: ## Lint with ruff
	uv run ruff check src tests

fmt: ## Format with ruff
	uv run ruff format src tests && uv run ruff check --fix src tests

typecheck: ## Static type check
	uv run mypy src

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
