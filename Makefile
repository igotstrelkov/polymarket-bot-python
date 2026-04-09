.PHONY: install test test-unit test-integration lint typecheck migrate run

install:
	poetry install

test:
	poetry run pytest -x

test-unit:
	poetry run pytest tests/unit/ -v

test-integration:
	poetry run pytest tests/integration/ -v

lint:
	poetry run ruff check .

typecheck:
	poetry run mypy .

migrate:
	poetry run python -m scripts.migrate

run:
	poetry run python -m bot
