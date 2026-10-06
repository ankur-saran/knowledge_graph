# CLAUDE.md

`ubo-sentinel` screens an entity for sanctions exposure through its ownership graph. The build follows `BUILD_PLAN.md` one step at a time. The product requirements are in `ubo-sentinel-PRD.md`, one directory above this repo.

## How to work here

- Build one step of `BUILD_PLAN.md` at a time (`/build-step N`). A step is done when its "Verifiable" command has been run and its real output reported.
- Each step registers the CLI command it verifies with, and adds that command to the permission matrix in `ontology/ontology.yaml`.
- Run every command from the repository root; paths such as `ontology/`, `fixtures/` and `var/` are relative to it.

## Conventions

- **Determinism.** Same inputs and same snapshot set give a byte-identical canonical decision payload. No ids from wall-clock time or random UUIDs. Sort every collection before serialising. `canonical_json` is sorted keys, no whitespace, UTF-8.
- **Canonical decision payload.** The `Decision` record minus `created_at`, `status`, `llm_annotations`, `query` and the reviewer fields (`reviewed_by`, `review_reason`, `reviewed_at`), produced only by `Decision.canonical_payload()`. What the caller typed (the query, a snapshot alias, the role) is in the audit event, never in the payload: `Decision.snapshot_set_id` is the resolved id.
- **Percentages are `Decimal`**, never `float` (DuckDB `DECIMAL(7,4)`); `canonical_json` writes them as strings quantised to 4 places.
- **Hashed ids** are `sha256` over a `canonical_json` list, never string concatenation. `decision_id` uses the resolved per-dataset `snapshot_ids`, not the set alias, and includes the `graph_digest` and `PIPELINE_VERSION`.
- **Stable bytes.** Snapshot ids hash file bytes. Files under `fixtures/`, `eval/`, `rules/` and `ontology/` are UTF-8 without BOM with LF endings (`.gitattributes`).
- **Reasons and gaps are closed sets.** `ReasonCode` and `GapCode` in `models/evidence.py`. A truncated traversal is a `DEPTH_LIMIT_REACHED` gap, and a node linked by name to a record with owners of its own is a `POSSIBLE_DUPLICATE` gap: both always decide, so neither is a silent `CLEAR`.
- **One name scorer.** Entity linking and query-time resolution both use `name_similarity()` in `pipeline/name_match.py`.
- **Provenance.** Every node, edge and sanction carries one `Provenance(source, source_record_id, snapshot_id, as_of, confidence)` in Bronze, Silver, Gold and the Pydantic models.
- **Edge direction.** `OWNS` is owner → asset. `CONSOLIDATED_BY` is child → parent. `CONTROLS` is controller → controlled. Gold stores every edge's `upper_id` (owner, parent or controller) and `lower_id`; only `pipeline/gold.py` and code under `graph/` reason about raw direction.
- **Snapshot set.** A `snapshot_set_id` names a manifest row that resolves to per-dataset `snapshot_id`s (a dataset is one published file or feed). `--snapshot` takes a set id (`fixtures`, `latest`, or an explicit id). Both ids are content-derived: a snapshot id from the file's bytes, a set id from its members. A set has a family (`fixtures` or `real`) and inherits missing datasets only from its own family. `latest` is the newest set; `fixtures` is the newest fixture set that one complete directory supplied.
- **Bronze is append-only.** `_raw` is the source record as published; Silver parses it. An ingest writes everything in one transaction or nothing. The only wall-clock value is `bronze_snapshots.ingested_at`, and no id or hash reads it.
- **Silver is built per snapshot set** and is derived: `ubo normalize --rebuild` replaces it. `silver_builds` records the engine version, the parameters and a digest of the rows; a set built by other code is refused, not reused. Raise `TRANSFORM_VERSION` in `pipeline/transformer.py` when a change alters the rows.
- **Merge on identifiers, link on names.** Records that share an LEI, or a registration authority and number, are one entity; a registration join that would put two LEIs or two entity types in one entity is refused. Entities with similar names are never merged: they get a `silver_entity_links` row, and a designation reaches the linked entity at the link's confidence, one link away. A registry's record describes a merged entity before a list's record does (`pipeline/sources.py`).
- **Source formats stop at the mapper.** `pipeline/mappers.py` turns each dataset's Bronze `_raw` into the typed staging tables; nothing after staging knows a source's format.
- **Roles.** `analyst`, `reviewer`, `auditor`, `engineer`. Every CLI command calls `require_role()` from `cli/app.py`, which reads the matrix in `ontology/ontology.yaml`. `review` requires an explicit `--role reviewer`.
- **Gold is built per snapshot set** from Silver and holds only what Silver lacks (edge ends, node degrees, links from both ends, name blocks). `gold_builds` records the Silver digest it was built from and a `gold_digest`; a graph built from an earlier Silver is stale and `ubo build-graph` rebuilds it.
- **Queries.** Graph data is read only through the typed functions in `graph/queries.py`, each registered in `QUERIES` by `@query`. No free-form queries. Every query takes a `GraphStore`, which is bound to one connection and one built snapshot set.
- **Store and view.** The screening pipeline reads the `GraphStore` unmasked, so a decision does not depend on who asked. Anything shown to a person, exported or sent to a model goes through `GraphView(role)`, or for a decision through `mask_decision()`, which reads what to mask from the memo's own entity table; what is masked comes from the role's `masked_fields` in the ontology.
- **Connections.** DuckDB lets a file have one writer or readers, never both, and one process cannot mix the two. Open a store for one operation and close it; pass an existing connection where one is already open.
- **Agents.** A screen is seven agents run in order by the `Supervisor`; no model. Each declares the `PipelineState` fields it `requires` and `provides`, and `AGENT_CONTRACTS.md` must equal those declarations (`tests/unit/test_agent_contracts.py`). Raise `PIPELINE_VERSION` in `agents/base.py` when a change alters the decision a screen produces (how Intake selects, what the memo holds, what the guardrail checks).
- **One id, one decision.** Screening the same inputs again computes the decision and compares it with the one on record: equal, the recorded one is returned and nothing is appended; different, `DeterminismViolation`.
- **Failing closed.** A failure never lowers a recommendation and never produces a `CLEAR`. A guardrail fault raises the recommendation to at least `REVIEW`. An agent that raises is a `SCREEN_FAILED` event and no decision: it is shown as `REVIEW / PIPELINE_ERROR` and is not cached.
- **Memo.** `models/memo.py` `Memo`: names are in the `entities` table only; claims, paths and matches refer to entities and edges by id, and every claim has citations. `build_memo()` is pure. A stored memo is unmasked.
- **Two guardrails.** The decision guardrail reads only role-independent things and may change a decision. The render guardrail reads what one role is shown and may only withhold output. A warning names the kind of fault, never a value.
- **Audit log.** `var/audit/events.jsonl` is the record; `var/decisions/` is a copy. An event's hash is taken over its JSON-mode dump without `hash`, and verification recomputes from the parsed line. Appending needs a role that may write (`require_write()` in `models/ontology.py`), not an open graph.
- **Runtime output** goes to `var/` (gitignored). Env `UBO_VAR` moves the audit log and the decision copies; tests and `ubo eval` use a temporary directory.
- **LLM use** is optional, advisory and recorded. Each LLM feature has its own flag, off by default. No LLM output can alter a recommendation, a decision's status or the canonical payload. Replay reads stored LLM text and never calls the model.
- **No network in tests.** Downloading is separate from ingesting.
- **Rule thresholds** live in the rule-pack YAML, never in Python. Every key of a pack is required. `rule_pack_hash` is over the parsed values and `RULES_VERSION`; raise `RULES_VERSION` in `rules/pack.py` when a change to the engine alters what a pack concludes.
- **The rule engine is pure.** `RuleEngine.evaluate()` reads one `SubGraph` and its sanction matches: no database, no clock. It uses only the nodes above the target, classifies a match by `designated_entity_id` (not `match_type`), reads all edges of one pair as one hop (never added together), and counts exposure from the last blocked entity on a path. Aggregates and exposure are exact fractions; a figure is rounded only when it is reported, never before it is compared.
- **Gold scenarios are exact.** The evaluator requires reasons, both sets and the gaps to be equal; expected paths are a minimum. A new rule that adds a gap or a reason changes existing scenarios.

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
| Models package | `src/ubo_sentinel/models/` |
| Ontology file | `ontology/ontology.yaml` (loader: `load_ontology()` in `models/ontology.py`) |
| Loader directory | `src/ubo_sentinel/pipeline/loaders/` (registry: `LOADERS` in its `__init__.py`; reference loader: `fixture_loader.py`, tests in `tests/unit/test_bronze.py`) |
| Bronze module | `src/ubo_sentinel/pipeline/bronze.py` (`SnapshotManager`, `resolve_set()`, `transaction()`) |
| Database | `var/ubo.duckdb`, opened with `connect()` in `src/ubo_sentinel/pipeline/db.py`; env `UBO_DB` overrides the path (tests use a temp file) |
| Silver module | `src/ubo_sentinel/pipeline/silver.py` (tables, `silver_digest()`, `read_entities()` and the other readers; tests in `tests/unit/test_silver.py`) |
| Silver mapping module | `src/ubo_sentinel/pipeline/mappers.py` (`MAPPERS`: dataset → staging columns); source ranks in `pipeline/sources.py` |
| Silver transformer | `src/ubo_sentinel/pipeline/transformer.py` (`build_silver()`); merge and link rules in `pipeline/entity_linking.py` |
| Gold module | `src/ubo_sentinel/pipeline/gold.py` (`build_gold()`, `gold_digest()`, `is_built()`; tests in `tests/unit/test_gold.py`) |
| Build-graph command | `uv run ubo build-graph [--snapshot SET]` (`src/ubo_sentinel/cli/build_graph_cmd.py`) |
| Graph store | `src/ubo_sentinel/graph/store.py` (`GraphStore.open(snapshot, con=None)`, `GraphNotBuilt`) |
| Query API | `src/ubo_sentinel/graph/queries.py` (`QUERIES` registry, `@query`); traversal in `graph/projection.py`; tests in `tests/unit/test_graph.py` |
| Entity resolution | `src/ubo_sentinel/graph/entity_resolution.py` (`resolve()`, `evaluate_labelled()`) |
| Role views | `src/ubo_sentinel/graph/views.py` (`GraphView`; tests in `tests/unit/test_views.py`) |
| Graph models | `src/ubo_sentinel/models/graph.py` (`SubGraph`, `EntityMatch`, `SanctionMatch`, `OwnerEdge`) |
| Shared test fixtures | `tests/conftest.py` (`database`, `con`, `ontology`, `gold`, `graph_t0`, `build_graph()`, `PACK`: the shipped rule pack); hand-built subgraphs in `tests/graphs.py` |
| Normalize command | `uv run ubo normalize [--snapshot SET] [--rebuild]` (`src/ubo_sentinel/cli/normalize_cmd.py`) |
| Ingest command | `uv run ubo ingest --source <name> [--path DIR] [--base SET]` (`src/ubo_sentinel/cli/ingest_cmd.py`) |
| Fixture directory | `fixtures/snapshot_t0/` (changed sanctions list: `fixtures/snapshot_t1/`); format in `fixtures/README.md` |
| Fixture row schemas and reader | `src/ubo_sentinel/models/fixture_rows.py` (`read_rows()`) |
| Fixture-integrity test | `uv run pytest tests/unit/test_fixtures.py -v` |
| Name-matching module | `src/ubo_sentinel/pipeline/name_match.py` |
| Rule-pack directory | `rules/` |
| Reference rule pack | `rules/ofac.yaml` (schema and loader: `RulePack`, `load_rule_pack()`, `pack_path()` in `src/ubo_sentinel/rules/pack.py`) |
| Rule engine | `src/ubo_sentinel/rules/engine.py` (`RuleEngine.evaluate()`); result model `src/ubo_sentinel/models/rule_result.py`; tests in `tests/unit/test_rules.py`, `tests/unit/test_rules_hypothesis.py` |
| Gold-scenario file | `eval/gold_scenarios.yaml` (schema and loader: `models/gold_scenario.py`) |
| Labelled ER set | `eval/er_labelled.csv` |
| Evaluator command | `uv run ubo eval --suite all` (suites: `rules`, `pipeline`, `er`; `src/ubo_sentinel/cli/eval_cmd.py`; comparison in `src/ubo_sentinel/rules/evaluator.py`). Needs a built graph: run ingest, normalize and build-graph first |
| Agents | `src/ubo_sentinel/agents/` (`base.py`: `ScreenRequest`, `PipelineState`, `ScreenContext`, `BaseAgent`, `PIPELINE_VERSION`; `supervisor.py`: `Supervisor`, `PIPELINE`; one module per agent; tests in `tests/unit/test_agents.py`) |
| Agent contracts | `AGENT_CONTRACTS.md` (checked by `tests/unit/test_agent_contracts.py`) |
| Memo | `src/ubo_sentinel/models/memo.py` (`Memo`, `Claim`, `Citation`, `DecisionView`); built and rendered in `agents/explainer.py`; template `src/ubo_sentinel/templates/memo.md.j2`; tests in `tests/unit/test_memo.py` |
| Guardrails | `src/ubo_sentinel/agents/guardrail.py` (`check_memo()`, `check_prose()`, `check_render()`; tests in `tests/unit/test_guardrail.py`) |
| Audit log | `src/ubo_sentinel/audit/log.py` (`AuditLog`: `append()`, `verify()`, `decision()`; tests in `tests/unit/test_audit_log.py`) |
| Screen command | `uv run ubo screen "<name or LEI>" [--snapshot SET] [--role analyst\|reviewer] [--pick N \| --target-id ID] [--json]` (`src/ubo_sentinel/cli/screen_cmd.py`; tests in `tests/unit/test_screen_cli.py`) |
| Pipeline evaluation | `uv run ubo eval --suite pipeline` (`src/ubo_sentinel/agents/evaluator.py`) |
| Runtime output directory | `var/` (env `UBO_VAR` overrides it for the audit log and decisions) |
