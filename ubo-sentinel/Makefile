.PHONY: install test lint ingest screen eval refresh

Q ?= Acme Trading FZE

install:
	uv sync --all-extras

# Coverage gate lives here only (not in pytest addopts), so single-file test
# commands are not failed by it.
test:
	uv run pytest --cov=src/ubo_sentinel/rules --cov=src/ubo_sentinel/graph --cov-fail-under=85

lint:
	uv run ruff check .
	uv run ruff format --check .

# Ingest + normalize + build-graph on fixtures (commands arrive in Steps 3-5).
ingest:
	uv run ubo ingest --source fixtures
	uv run ubo normalize
	uv run ubo build-graph

# Step 7. Usage: make screen Q="Entity name"
screen:
	uv run ubo screen "$(Q)" --snapshot fixtures

# Step 6.
eval:
	uv run ubo eval --suite all

# Step 10: download real data to var/downloads/ and ingest it.
refresh:
	@echo "refresh is implemented in Step 10" && exit 1
