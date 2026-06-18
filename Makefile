.PHONY: install lint test format run
install:
	pip install -e ".[dev]"
format:
	ruff format kairos_risk tests
lint:
	ruff check kairos_risk tests
test:
	pytest -q
run:
	python -m kairos_risk
