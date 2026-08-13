UV ?= uv

.PHONY: install lock format lint typecheck security test check build run

install:
	$(UV) sync --locked

lock:
	$(UV) lock

format:
	$(UV) run --locked ruff format kairos_risk tests

lint:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

typecheck:
	$(UV) run --locked mypy kairos_risk

security:
	$(UV) run --locked bandit -q -r kairos_risk -x tests

test:
	$(UV) run --locked pytest -q --tb=short

check: lint typecheck security test build

build:
	$(UV) build --no-sources

run:
	$(UV) run --locked python -m kairos_risk
