# CLAUDE.md

`ubo-sentinel` screens an entity for sanctions exposure through its ownership graph. The build follows `BUILD_PLAN.md` one step at a time. The product requirements are in `ubo-sentinel-PRD.md`, one directory above this repo.

## How to work here

- Build one step of `BUILD_PLAN.md` at a time (`/build-step N`). A step is done when its "Verifiable" command has been run and its real output reported.
- Each step registers the CLI command it verifies with, and adds that command to the permission matrix in `ontology/ontology.yaml`.
- Run every command from the repository root; paths such as `ontology/`, `fixtures/` and `var/` are relative to it.

## Conventions

- **Determinism.** Same inputs and same snapshot set give a byte-identical canonical decision payload. No ids from wall-clock time or random UUIDs. Sort every collection before serialising. `canonical_json` is sorted keys, no whitespace, UTF-8.
- **Canonical decision payload.** The `Decision` record minus `created_at`, `prev_hash`, `hash`, `llm_annotations` and reviewer fields.
- **Provenance.** Every node, edge and sanction carries one `Provenance(source, source_record_id, snapshot_id, as_of, confidence)` in Bronze, Silver, Gold and the Pydantic models.
- **Edge direction.** `OWNS` is owner → asset. `CONSOLIDATED_BY` is child → parent. `CONTROLS` is controller → controlled. Only code under `graph/` reasons about raw direction.
- **Snapshot set.** A `snapshot_set_id` names a manifest row that resolves to per-source `snapshot_id`s. `--snapshot` takes a set id (`fixtures`, `latest`, or an explicit id).
- **Roles.** `analyst`, `reviewer`, `auditor`, `engineer`. Every CLI command calls `require_role()` from `cli/app.py`, which reads the matrix in `ontology/ontology.yaml`. `review` requires an explicit `--role reviewer`.
- **Queries.** Agents, CLI and UI read graph data only through the typed functions in `graph/queries.py` and through `GraphView(role)`. No free-form queries.
- **Runtime output** goes to `var/` (gitignored).
- **LLM use** is optional, advisory and recorded. Each LLM feature has its own flag, off by default. No LLM output can alter a recommendation, a decision's status or the canonical payload. Replay reads stored LLM text and never calls the model.
- **No network in tests.** Downloading is separate from ingesting.
- **Rule thresholds** live in the rule-pack YAML, never in Python.

## Project facts

Read by the skills in `.claude/skills/`. A value of "not yet created" is filled in by the step named beside it.

| Fact | Value |
|---|---|
| Build-plan file | `BUILD_PLAN.md` |
| Package root | `src/ubo_sentinel/` |
| Test directory | `tests/unit/`, `tests/integration/` |
| Test command | `uv run pytest` |
| Full test command (with coverage gate) | `make test` |
| Lint command | `uv run ruff check .` and `uv run ruff format --check .` |
| CLI entry point | `src/ubo_sentinel/cli/app.py` (run as `uv run ubo`) |
| Ontology file | `ontology/ontology.yaml` |
| Loader directory | `src/ubo_sentinel/pipeline/loaders/` — not yet created (Step 3) |
| Bronze module | `src/ubo_sentinel/pipeline/bronze.py` — not yet created (Step 3) |
| Silver module | `src/ubo_sentinel/pipeline/silver.py` — not yet created (Step 4) |
| Silver mapping module | `src/ubo_sentinel/pipeline/transformer.py` — not yet created (Step 4) |
| Ingest command | `uv run ubo ingest --source <name>` — not yet created (Step 3) |
| Fixture directory | `fixtures/snapshot_t0/` (changed sanctions list: `fixtures/snapshot_t1/`) |
| Fixture-integrity test | `uv run pytest tests/unit/test_fixtures.py -v` — not yet created (Step 2) |
| Rule-pack directory | `rules/` |
| Reference rule pack | `rules/ofac.yaml` — not yet created (Step 6) |
| Gold-scenario file | `eval/gold_scenarios.yaml` — not yet created (Step 2) |
| Evaluator command | `uv run ubo eval --suite all` — not yet created (Step 6) |
| Runtime output directory | `var/` |
