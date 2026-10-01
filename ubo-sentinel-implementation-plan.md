# ubo-sentinel — Detailed Implementation Plan

> Revision 3 (2026-10-01) — Steps 0–13 reconciled against `ubo-sentinel-PRD.md` v1.0; Steps 13.4, 13.5, 14 and 15 (LLM assists, investigator, document extraction) are extensions beyond PRD v1.0 and need the amendments listed at the end. This document is the PRD's `BUILD_PLAN.md` companion; copy it into the repo under that name in Step 0.

## Context

`ubo-sentinel` is a Knowledge-Graph-driven Beneficial Ownership & Sanctions Exposure Screening tool. A KYC analyst provides an entity name or LEI; the system walks the ownership graph (configurable depth, default 5 hops), applies the OFAC 50 Percent Rule via fix-point aggregation, generates a fully-cited risk memo, and gates every final decision behind a human reviewer. The architecture is a DuckDB medallion pipeline (Bronze → Silver → Gold) feeding a per-screen NetworkX ego-graph, orchestrated by a typed agent pipeline. The build is local-first, keyless, and deterministic.

Every step below is the **smallest increment that produces a runnable, verifiable artifact** — a working MVP at each stage.

### Cross-cutting conventions (apply to every step)

- **Determinism.** Same inputs + same snapshot set ⇒ byte-identical *canonical decision payload*. No ids derived from wall-clock time or random UUIDs; all collections sorted before serialization; `canonical_json` = sorted keys, no whitespace, UTF-8.
- **Canonical decision payload.** The `Decision` record minus `created_at`, `prev_hash`, `hash`, `llm_annotations` and reviewer fields. This is what determinism and replay tests compare.
- **Provenance.** Every node, edge and sanction carries one `Provenance(source, source_record_id, snapshot_id, as_of, confidence)` in Bronze, Silver, Gold and the Pydantic models.
- **Edge direction.** `OWNS` is owner → asset. `CONSOLIDATED_BY` is child → parent. `CONTROLS` is controller → controlled. The query API normalises all three to "owner/controller of X"; nothing outside `graph/` reasons about raw direction.
- **Snapshot set.** A screen spans several source snapshots (GLEIF L1, RR, RepEx, OpenSanctions, or fixtures). A `snapshot_set_id` names a manifest row that resolves to the per-source `snapshot_id`s. CLI `--snapshot` takes a set id (`fixtures`, `latest`, or an explicit id); `Decision.snapshot_ids` stores the resolved per-source ids.
- **Roles (simulated locally).** `analyst`, `reviewer`, `auditor`, `engineer`, defined with a permission matrix in `ontology/ontology.yaml`. Every CLI command checks the matrix; each command has a sensible default `--role` (`screen` → analyst, pipeline commands → engineer); `review` requires an explicit `--role reviewer`.
- **Runtime output** lives in a gitignored `var/` directory (`var/ubo.duckdb`, `var/audit/events.jsonl`, `var/decisions/`, `var/rescreen_queue.jsonl`, `var/downloads/`).
- **LLM use is optional, advisory and recorded.** Every LLM feature (Steps 13, 14 and 15) is behind its own flag, off by default, with the keyless path unchanged. No LLM output can alter a recommendation, a decision's status or the canonical payload. Every LLM output is stored in the audit log, and replay reads the stored text; it never calls the model again.
- **No network in tests.** Downloading is separate from ingesting; the test suite and fixture mode never open a socket.

---

## Step 0 — Project Skeleton
**Verifiable:** `uv run python -c "import ubo_sentinel; print('ok')"` succeeds; `uv run ubo --help` lists the (empty) command groups.

### 0.1 Initialize repo and tooling
- `uv init --package ubo-sentinel --python 3.12` (packaged `src/` layout)
- Add `.python-version` pinned to 3.12
- `pyproject.toml` dependency groups:
  - **core:** `duckdb`, `networkx`, `pydantic>=2`, `rapidfuzz`, `typer`, `pyyaml`, `python-dotenv`, `jinja2`, `httpx`
  - **ui:** `streamlit`, `pyvis`
  - **dev:** `pytest`, `hypothesis`, `pytest-cov`, `ruff`
  - **optional:** `anthropic`, `neo4j`
- `[project.scripts] ubo = "ubo_sentinel.cli.app:app"`
- `Makefile` targets: `install`, `test` (with coverage gate, see 9.2), `lint`, `ingest` (ingest + normalize + build-graph on fixtures), `screen`, `eval`, `refresh`
- `src/ubo_sentinel/__init__.py` (exports `__version__`, recorded in every decision as `engine_version`)
- `.gitignore` including `var/`

### 0.2 CLI skeleton (`src/ubo_sentinel/cli/app.py`)
Create the Typer app and a `require_role()` helper now. Each later step registers its own command in the step that first needs it, so every step's verification command exists when that step is done.

### 0.3 Companion docs (stubs now, filled in the step noted)
- `CLAUDE.md` — project conventions, plus the `## Project facts` section the skills read (0.5)
- `ONTOLOGY.md` — human-readable ontology (Step 1)
- `AGENT_CONTRACTS.md` — typed input/output per agent (Step 7)
- `BUILD_PLAN.md` — this document
- `RUNBOOK.md` — demo script (Step 11)
- `README.md` — must carry: "not a production sanctions-screening system; not legal advice; rule packs must be validated by compliance counsel" and the OpenSanctions **CC BY-NC 4.0** notice (demo/personal use only)

### 0.4 Directory skeleton
```
ubo-sentinel/
├── .claude/
│   └── skills/          # build-step, add-data-source, add-rule-pack, add-gold-scenario (0.5)
├── src/ubo_sentinel/
│   ├── __init__.py
│   ├── models/          # Pydantic data models
│   ├── graph/           # Ego-graph extraction, typed query API, role views
│   ├── pipeline/        # Bronze → Silver → Gold, loaders, differ
│   ├── agents/          # Agent implementations
│   ├── rules/           # Rule engine (Python)
│   ├── audit/           # Hash-chained event log + replay
│   ├── templates/       # memo.md.j2
│   └── cli/             # Typer CLI
├── ontology/
│   └── ontology.yaml
├── fixtures/
│   ├── snapshot_t0/     # entities, relationships, sanctions, repex CSVs
│   └── snapshot_t1/     # sanctions.csv only (changed list, for Step 12)
├── eval/
│   ├── gold_scenarios.yaml
│   ├── er_labelled.csv
│   └── real_smoke.yaml  # Step 10
├── rules/
│   └── ofac.yaml        # uk_ofsi.yaml / eu.yaml arrive in Step 13 (illustrative only)
├── tests/
│   ├── unit/
│   └── integration/
├── var/                 # gitignored runtime output
├── pyproject.toml
├── Makefile
└── *.md                 # companion docs
```

### 0.5 Claude Code skills (`.claude/skills/`)
Build-time helpers for Claude Code; they are not part of the running application. Each skill is a folder containing a `SKILL.md` with `name` and `description` frontmatter followed by the procedure. All four are authored in the repo so they are versioned with the code.

| Skill | Scope | What it does |
|---|---|---|
| `build-step` | Generic | Takes a step number. Reads that step from the build plan, implements only that step, runs its "Verifiable" command, reports the actual output, and stops. Does not start the next step. |
| `add-data-source` | Generic (any medallion pipeline) | Takes a source name. Adds a loader, the Bronze table, the Silver mapping with provenance, a small fixture sample and tests; registers the source in the ingest command. |
| `add-rule-pack` | Project-specific | Adds a rule-pack YAML using only existing config keys, a gold-scenario file for it, and runs the evaluator; confirms the OFAC gold scenarios are unchanged. |
| `add-gold-scenario` | Project-specific | Adds fixture rows for one scenario, the gold entry with expected paths and gaps, and runs the fixture-integrity test and the evaluator. |

Rules that keep the generic skills reusable:
- **No project facts in the skill body.** A generic skill names no package, path, command or domain term. It reads them from a `## Project facts` section in `CLAUDE.md`: build-plan file, package root, test command, lint command, loader directory, Bronze/Silver module paths, fixture directory, evaluator command.
- **Procedure and stop condition only.** Each skill states its inputs, the ordered steps, what "done" means, and what to report. Domain rules (OFAC semantics, ontology) stay in the project docs the skill points to.
- **One job per skill.** A skill that needs another's outcome says so and stops; it does not chain.
- **Reuse path.** To use `build-step` or `add-data-source` in another project, copy the folder to `~/.claude/skills/` (available in every project) or package both in a plugin to share them; the target project only needs its own `## Project facts` section. The two project-specific skills stay in this repo.

`CLAUDE.md` gains the `## Project facts` section in this step, with paths filled in as later steps create them.

**Verifiable (skills):** each `SKILL.md` has valid frontmatter and appears in Claude Code's skill list; `/build-step 1` is used to build Step 1 as the first real test of the skill.

---

## Step 1 — Pydantic Data Models + Ontology
**Verifiable:** `uv run pytest tests/unit/test_models.py tests/unit/test_ontology.py -v` passes (round-trip serialization; ontology validator accepts the shipped YAML and rejects a malformed one).

### 1.1 Core domain models (`src/ubo_sentinel/models/`)
- `provenance.py` — `Provenance(source, source_record_id, snapshot_id, as_of, confidence)`
- `entity.py` — `Entity(id, lei, registration_authority_id, registration_number, legal_name, aliases: list[str], jurisdiction, status, entity_type: Literal["LegalEntity","Person"], repex_reason, provenance)`
- `relationship.py` — `Relationship(id, subject_id, object_id, rel_type: Literal["OWNS","CONSOLIDATED_BY","CONTROLS"], pct: float|None, basis, is_ultimate: bool, provenance, supporting_records: list[Provenance])`
- `sanction.py` — `Sanction(id, entity_id, program, list_date, list_source, is_active: bool, match_type, match_confidence: float, provenance)`
- `decision.py` — `Decision(decision_id, query, target_id, recommendation: Literal["CLEAR","REVIEW","ESCALATE"], status: Literal["RECOMMENDED","APPROVED","OVERRIDDEN","ESCALATED"], blocked_set, possibly_blocked_set, paths, effective_exposure, data_gaps, guardrail_warnings, memo_json, llm_annotations, snapshot_set_id, snapshot_ids, rule_pack_id, rule_pack_hash, engine_version, max_depth, created_at)`
- `audit_event.py` — `AuditEvent(seq, decision_id, event_type: Literal["RECOMMENDED","APPROVED","OVERRIDDEN","ESCALATED"], actor, role, note, payload, created_at, prev_hash, hash)`
- `ontology.py` — `OntologyConfig` Pydantic model that validates `ontology/ontology.yaml`

`decision_id = sha256(target_id + snapshot_set_id + rule_pack_hash + engine_version + max_depth)[:16]` — deterministic. Screening identical inputs again returns the existing decision.

### 1.2 Ontology YAML (`ontology/ontology.yaml`) + `ONTOLOGY.md`
Defines: node labels (`LegalEntity`, `Person`); edge types with their direction; how designations are represented (a `graph_sanctions` table keyed by node id — not edges); required provenance fields; the four roles and their permission matrix (which commands, which fields visible, decision rights). `OntologyValidator` loads it and raises on schema violations.

---

## Step 2 — Synthetic Fixtures
**Verifiable:** `uv run pytest tests/unit/test_fixtures.py -v` validates referential integrity directly from the CSVs (no pipeline code needed) and that every PRD §9 recommendation branch is covered by at least one gold scenario.

### 2.1 Fixture CSV files (`fixtures/snapshot_t0/`)
- `entities.csv` — ~40 entities (corps, holdings, persons), with registration numbers and aliases
- `relationships.csv` — `OWNS`, `CONSOLIDATED_BY` (null-%) and `CONTROLS` edges
- `sanctions.csv` — designations across 2 programs, including one inactive (delisted) record
- `repex.csv` — reporting exceptions

`fixtures/snapshot_t1/sanctions.csv` — same list with one designation added, one removed, one changed (drives Step 12).

Scenario coverage:

| Scenario | Expected | Tests |
|---|---|---|
| `SCEN-01` | `CLEAR` | No blocked owner, full % known |
| `SCEN-02` | `ESCALATE` | Direct designation of target |
| `SCEN-03` | `ESCALATE` | Two blocked owners each 30 % → aggregate ≥ 50 % |
| `SCEN-04` | `REVIEW` | Null-% consolidation edge to a blocked parent |
| `SCEN-05` | `REVIEW` | Near-miss (42 % aggregate) |
| `SCEN-06` | `ESCALATE` | 3-hop indirect blocking via derived-blocked intermediates |
| `SCEN-07` | `ESCALATE` | Exactly 50.0 % (boundary of `>=`) |
| `SCEN-08` | `REVIEW` | Single blocked owner at 30 %: aggregate < 40, effective exposure ≥ 25 |
| `SCEN-09` | `REVIEW` | FAQ 401 negative case: blocked party holds 40 % of an intermediate that owns 100 % of target — intermediate is **not** derived-blocked, target is not `ESCALATE`; effective exposure 40 % |
| `SCEN-10` | `CLEAR` | Same shape as SCEN-09 with 20 % — below every threshold |
| `SCEN-11` | `REVIEW` | Control-only (`CONTROLS`) link from a blocked party |
| `SCEN-12` | `REVIEW` | Blocked owner at 20 % plus a reporting exception on the target |
| `SCEN-13` | `REVIEW` | Lapsed-LEI intermediate on the path to a blocked owner |
| `SCEN-14` | `REVIEW` | Majority owner matched to a designation only in the review band |
| `SCEN-15` | `ESCALATE` | Ownership cycle (A ↔ B) with a blocked majority owner — must terminate |
| `SCEN-16` | `CLEAR` | Ambiguous name → disambiguation required, then clear |
| `SCEN-17` | `CLEAR` | Owner's designation is inactive (delisted) |
| `SCEN-18` | `REVIEW` | Same 30 % stake reported by two sources — must not be double-counted to 60 % |

### 2.2 Gold scenarios (`eval/gold_scenarios.yaml`)
Each entry: `id`, `query_entity`, `pick` (for disambiguation), `expected_recommendation`, `expected_paths`, `expected_gaps`, `rationale`.

### 2.3 Labelled ER set (`eval/er_labelled.csv`)
`query_name, jurisdiction, expected_entity_id` — name variants, transliterations and near-duplicates over the fixture entities; extended with real names in Step 10.

---

## Step 3 — Bronze Data Layer (DuckDB)
**Verifiable:** `uv run ubo ingest --source fixtures` prints table row counts and the snapshot ids; running it twice yields the same snapshot ids and no duplicate rows; `uv run pytest tests/unit/test_bronze.py` checks schema and row counts.

### 3.1 DuckDB Bronze schema (`src/ubo_sentinel/pipeline/bronze.py`)
- Tables: `bronze_entities`, `bronze_relationships`, `bronze_sanctions`, `bronze_repex`
- All tables include: `_source TEXT`, `_source_record_id TEXT`, `_snapshot_id TEXT`, `_ingested_at TIMESTAMP`, `_raw JSON`
- Tables are **append-only + immutable per snapshot**
- `SnapshotManager`: `snapshot_id = sha256(source + file_bytes)[:12]` (content-derived; real sources also record the publish date). Ingesting an already-present snapshot is a no-op.
- `snapshot_sets(snapshot_set_id, source, snapshot_id)` manifest table; `fixtures` and `latest` are aliases resolved here.

### 3.2 Fixture loader (`src/ubo_sentinel/pipeline/loaders/fixture_loader.py`)
- Reads `fixtures/snapshot_t0/*.csv` → bronze tables
- Validates column presence against the ontology
- Prints ingestion summary: source, snapshot_id, row counts per table

### 3.3 CLI: `ubo ingest --source [fixtures|gleif|opensanctions] [--path …]`
Registered here with only `fixtures` implemented; real sources are added in Step 10.

---

## Step 4 — Silver Canonical Layer
**Verifiable:** `uv run ubo normalize` completes; `uv run pytest tests/unit/test_silver.py` checks canonical columns, provenance on every row, one aggregation edge per (owner, asset), and ER precision@1 ≥ 95 % on `eval/er_labelled.csv`.

### 4.1 Silver schema (`src/ubo_sentinel/pipeline/silver.py`)
- `silver_entities(id, lei, registration_authority_id, registration_number, legal_name, aliases, jurisdiction, status, entity_type, repex_reason, + provenance columns)`
- `silver_relationships(id, subject_id, object_id, rel_type, pct, basis, is_ultimate, + provenance columns, supporting_records)`
- `silver_sanctions(id, entity_id, program, list_date, list_source, is_active, match_type, match_confidence, + provenance columns)`
- `silver_entity_links(entity_id_a, entity_id_b, match_type, confidence)`

### 4.2 Bronze → Silver transformer (`src/ubo_sentinel/pipeline/transformer.py`)
- **Cross-source entity resolution** (`pipeline/entity_linking.py`), priority: LEI exact → registration authority + number exact → `rapidfuzz` name + jurisdiction (with blocking). Exact-identifier matches merge into one canonical id. Fuzzy matches are **never merged**: they are written to `silver_entity_links` with a confidence, and a sanction reaches the linked entity with `match_confidence` = link confidence. This is the source of the review band.
- **Edge reconciliation:** exactly one `OWNS` edge per (owner, asset) for aggregation. When sources disagree, latest `as_of` wins and all source records are kept in `supporting_records`. `Σ pct` into an asset > 100 is recorded as a data gap.
- Null-% consolidation edges preserved with `basis = "accounting_consolidation"`, `pct = None`
- Reporting exceptions: `silver_entities.repex_reason` populated
- Lapsed/Retired LEI: `silver_entities.status = "LAPSED"`
- Every silver row carries full provenance

### 4.3 CLI: `ubo normalize`

---

## Step 5 — Gold Graph Layer + Query API + Role Views
**Verifiable:** `uv run ubo build-graph` completes; `uv run pytest tests/unit/test_graph.py tests/unit/test_views.py` traverses all fixture scenarios, confirms hop counts, cycle handling, and analyst PII masking / auditor write denial.

### 5.1 Gold tables (`src/ubo_sentinel/pipeline/gold.py`)
- `graph_nodes(id, label, attrs_json, provenance_json)` — from `silver_entities`
- `graph_edges(id, source_id, target_id, rel_type, pct, attrs_json, provenance_json)` — from `silver_relationships`
- `graph_sanctions(id, node_id, program, is_active, match_type, match_confidence, attrs_json, provenance_json)` — from `silver_sanctions`
- Indexed on `source_id` and `target_id` for frontier queries

### 5.2 Ego-graph extraction (`src/ubo_sentinel/graph/projection.py`)
The full graph **stays in DuckDB**. `extract_ego_graph(entity_id, snapshot_set, max_depth_up, max_depth_down)` runs an iterative frontier query (one indexed DuckDB query per hop, visited set carried between hops — terminates on cycles) and projects only the result into an `nx.MultiDiGraph`. Upward (owners/controllers) depth defaults to the rule pack's `max_depth`; downward (context) depth is capped separately (`max_depth_down`, default 1) so a large parent does not pull in thousands of subsidiaries.

### 5.3 Allow-listed query API (`src/ubo_sentinel/graph/queries.py`)
Eight typed functions — **no free-form queries**:
```python
def resolve_entity(name: str, lei: str | None, max_candidates: int) -> list[EntityMatch]
def get_ownership_subgraph(entity_id: str, max_depth_up: int, max_depth_down: int, snapshot_set_id: str) -> SubGraph
def get_sanctioned_nodes(subgraph: SubGraph, snapshot_set_id: str) -> list[SanctionMatch]
def get_reporting_exceptions(entity_id: str) -> list[RepexRecord]
def get_entity_by_id(entity_id: str) -> Entity | None
def get_direct_owners(entity_id: str) -> list[OwnerEdge]
def get_decision_by_id(decision_id: str) -> Decision | None
def get_affected_entities(changed_node_id: str, max_depth: int, snapshot_set_id: str) -> list[str]
```

### 5.4 Query-time entity resolution (`src/ubo_sentinel/graph/entity_resolution.py`)
Priority order: LEI exact match → registration number → `rapidfuzz` name + jurisdiction. Fuzzy candidates are pre-filtered in DuckDB (jurisdiction + shared name token) before scoring, so resolution scales to the full GLEIF set. Returns `EntityMatch(entity_id, confidence, match_type)`, sorted deterministically.

### 5.5 Role-scoped views (`src/ubo_sentinel/graph/views.py`)
`GraphView(role)` wraps `SubGraph` and is the only way agents, CLI and UI read graph data:
- `analyst` — full ownership graph; `Person` PII masked (`"[PII MASKED]"` + pseudonymised id)
- `reviewer` — full graph including person details
- `auditor` — read-only (writes raise); `time_travel(snapshot_set_id)` constructor
- `engineer` — pipeline metadata only; no decision rights

### 5.6 CLI: `ubo build-graph`

---

## Step 6 — OFAC Rule Engine
**Verifiable:** `uv run ubo eval --suite rules` → 100 % gold-scenario pass rate; `uv run pytest tests/unit/test_rules.py tests/unit/test_rules_hypothesis.py -v`.

### 6.1 Rule pack YAML (`rules/ofac.yaml`)
```yaml
id: ofac_50pct_v1
ownership_threshold_pct: 50
threshold_operator: ">="
aggregate_blocked_owners: true
match_threshold: 0.92
review_band_low: 0.75
presume_majority_for_consolidation: true   # null-% → possibly blocked → REVIEW, never silent CLEAR
gap_scope: on_path                         # on_path | any  (see 6.2 step 5)
review_exposure_pct: 25
near_miss_pct: 40
control_link_review: true
max_depth: 5
max_depth_down: 1
```
Nothing about the threshold is hard-coded in Python, so UK/EU packs (Step 13) are configuration.

### 6.2 Rule engine (`src/ubo_sentinel/rules/engine.py`)
- `RuleEngine(rule_pack_yaml)` loads and validates config; exposes `rule_pack_hash`
- `engine.evaluate(subgraph, sanction_matches, target_id) -> RuleResult`
  1. **Seed.** `B₀` = nodes with an **active** designation matched by exact identifier or with `match_confidence >= match_threshold`. `P₀` = nodes with an active designation matched in `[review_band_low, match_threshold)`. Inactive designations seed nothing.
  2. **Fix-point on `B` (the legal test).** For each entity *e* ∉ *B*: `agg(e) = Σ pct(o → e)` over direct `OWNS` owners *o* ∈ *B*. If `agg(e)` meets the threshold, add *e* to *B* (reason `DERIVED_50PCT`, contributing edges recorded). Repeat until stable. Stakes held through a non-blocked intermediate do not count.
  3. **Possibly-blocked set `P`.** A second fix-point: *e* ∉ *B* joins *P* if (a) it has a null-% consolidation/ownership edge to a parent in `B ∪ P` and `presume_majority_for_consolidation` is true, or (b) `Σ pct` over owners in `B ∪ P` meets the threshold.
  4. **Effective exposure (risk signal only, FR-09).** `max over paths Π pct` and `Σ over paths Π pct` from any node in *B* to the target, over simple paths bounded by `max_depth`. A path containing a null-% edge contributes "unknown" (recorded as a gap), never 0.
  5. **Data gaps.** Null-% edge, reporting exception, lapsed/retired LEI, review-band match, `Σ pct > 100`. With `gap_scope: on_path` a gap is *decision-relevant* only if it sits on a node or edge on a path between the target and a node in `B ∪ P` (endpoints included); all other gaps are listed in the memo as informational. `gap_scope: any` makes every gap in the upward subgraph decision-relevant (strict FR-10 reading; expect almost no `CLEAR` on real GLEIF data).
  6. **Recommendation.**
     - `ESCALATE` — target ∈ *B*.
     - `REVIEW` — target ∉ *B* and any of: target ∈ *P*; effective exposure ≥ `review_exposure_pct`; `agg(target)` ≥ `near_miss_pct`; a decision-relevant data gap; a `CONTROLS` edge into the target from a node in `B ∪ P`.
     - `CLEAR` — none of the above; result still lists traversal depth and sources checked.
- `RuleResult` carries every reason code with its contributing edges so the memo can cite them.

### 6.3 Property-based tests (`tests/unit/test_rules_hypothesis.py`)
Graph strategies generate arbitrary digraphs **including cycles and parallel edges**.
- Fix-point always terminates
- Adding a blocked owner never lowers recommendation severity
- Result is invariant under shuffling of node/edge input order (determinism)
- `B ∩ P = ∅`

### 6.4 Evaluator (`src/ubo_sentinel/cli/eval_cmd.py`) — `ubo eval [--suite rules|er|all]`
Loads `eval/gold_scenarios.yaml`, runs the engine on each, prints a pass/fail table plus the PRD §10 metrics: decision accuracy (target 100 %), evidence-path recall (≥ 98 %), ER precision@1 (≥ 95 %). Uncited-claim count and replay fidelity are added to the report in Steps 7 and 8.

---

## Step 7 — Agent Pipeline (Keyless, Deterministic)
**Verifiable:** `uv run ubo screen "Acme Trading FZE" --snapshot fixtures` returns a decision JSON in < 5 s with recommendation matching SCEN-01; running it twice yields an identical canonical payload.

### 7.1 Agent base + supervisor (`src/ubo_sentinel/agents/`)
- `BaseAgent`: `name`, `run(state) -> state` (pure function over pipeline state); typed input/output documented in `AGENT_CONTRACTS.md`
- `Supervisor` is a linear state machine (no LLM): `Intake → Traversal → SanctionsMatch → RuleEngine → Explainer → Guardrail → Audit(RECOMMENDED)`, then `HumanReview → Audit(transition)`. Any exception → `REVIEW` with reason.
- The supervisor can **pause and resume**: it returns `NEEDS_DISAMBIGUATION` (with candidates) or `AWAITING_REVIEW` instead of blocking on input.
- `PipelineState` (Pydantic): `query`, `role`, `candidates`, `target_id`, `subgraph`, `sanction_matches`, `rule_result`, `memo`, `decision`

### 7.2 Intake Agent (`agents/intake.py`)
- Calls `queries.resolve_entity()`
- Auto-selects only when the top candidate is an exact LEI/registration match, or the sole candidate with confidence ≥ `match_threshold`
- Otherwise returns `NEEDS_DISAMBIGUATION` (HITL #1); the caller resumes with a chosen id. CLI: interactive numbered list, or `--lei` / `--pick N` for non-interactive use.
- Zero candidates → `REVIEW` with reason `ENTITY_NOT_FOUND`

### 7.3 Traversal Agent (`agents/traversal.py`)
- Calls `queries.get_ownership_subgraph(entity_id, max_depth_up, max_depth_down, …)` with depths from the rule pack (overridable by `--max-depth`)
- Cycles handled by the visited set in the frontier query; logs hop count, node count, edge count, cycles found

### 7.4 Sanctions Match Agent (`agents/sanctions_match.py`)
- Calls `queries.get_sanctioned_nodes(subgraph, snapshot_set_id)` — reads the matches precomputed in Silver (exact identifier and fuzzy links)
- Returns `SanctionMatch(entity_id, match_type, confidence, is_active, sanction_records)` for every node; classification into blocked / review-band is the rule engine's job

### 7.5 Rule Engine Agent (`agents/rule_engine.py`)
- Thin wrapper: `RuleEngine(pack).evaluate(...)`; attaches `RuleResult` to state

### 7.6 Explainer Agent (`agents/explainer.py`)
- Builds `memo_json`: `target`, `recommendation`, `reasons[]`, `paths[]`, `effective_exposure`, `data_gaps[]` (decision-relevant vs informational), `sanctions_matches[]`, `traversal_depth`, `sources_checked[]`, disclaimer
- Every claim carries a citation: path + `source` + `source_record_id` + `as_of` — zero uncited facts
- Renders `memo_md` from a Jinja2 template (`templates/memo.md.j2`) through `GraphView(role)`; the stored `memo_json` is canonical and unmasked, masking is applied at render time for the requesting role
- No LLM in this step (LLM is Step 13 stretch)

### 7.7 Guardrail Agent (`agents/guardrail.py`)
- Validates `memo_json` against schema (Pydantic); checks every claim has a citation
- Checks every number/name in `memo_md` exists in `memo_json`
- Checks role masking on the rendered memo
- **Fails closed:** on any failure in keyless mode the recommendation is forced to `REVIEW` and `guardrail_warnings[]` is written to the decision. (When LLM prose is in play — Step 13 — a mismatch falls back to the template memo and still logs a warning.)
- Reports `uncited_claims` count to `ubo eval` (target 0)

### 7.8 Audit Agent (`agents/audit.py`, `audit/log.py`)
- The log is **event-sourced and append-only**: `var/audit/events.jsonl`, one `AuditEvent` per state transition. A screen appends `RECOMMENDED`; a review appends `APPROVED` / `OVERRIDDEN` / `ESCALATED`. Nothing is ever rewritten.
- Hash chain: `hash = sha256(prev_hash + canonical_json(record without the hash field))`; genesis `prev_hash = sha256("genesis")`
- `var/decisions/<decision_id>.json` is a derived convenience view rebuilt from the log, not a second source of truth
- The `RECOMMENDED` event's payload is the full decision including snapshot ids, rule-pack hash and engine version (everything replay needs)

### 7.9 CLI: `ubo screen <query>` (`src/ubo_sentinel/cli/screen_cmd.py`)
- `--snapshot [fixtures|latest|<set id>]` (default: fixtures)
- `--role [analyst|reviewer]` (default: analyst)
- `--rule-pack [ofac]` (default: ofac; `uk_ofsi` / `eu` accepted from Step 13)
- `--max-depth N`, `--lei`, `--pick N`
- Runs the pipeline up to `AWAITING_REVIEW`; prints decision summary + memo

---

## Step 8 — Review, Audit and Replay CLI
**Verifiable:** on fixtures — `ubo review` transitions a decision; `ubo audit verify` prints "Chain intact (N records)"; `ubo audit replay <decision_id>` prints "Replay identical".

### 8.1 `ubo review <decision_id>` (`src/ubo_sentinel/cli/review_cmd.py`)
- Rebuilds the decision from the log; displays memo + recommendation through `GraphView(reviewer)`
- Prompts: `[A]pprove / [O]verride (reason) / [E]scalate` (also `--action` / `--reason` for non-interactive use)
- Enforced state machine: only `RECOMMENDED → APPROVED | OVERRIDDEN | ESCALATED`; requires `--role reviewer`; override requires a non-empty reason; a decision in a terminal state cannot be reviewed again
- Appends the transition event via the Audit Agent

### 8.2 `ubo audit verify` (`src/ubo_sentinel/cli/audit_cmd.py`)
- Recomputes the hash chain over `var/audit/events.jsonl`; prints integrity status and the first broken record if any

### 8.3 `ubo audit replay <decision_id>` (FR-13)
- Loads the `RECOMMENDED` event; opens `GraphView.time_travel(snapshot_set_id)`; re-runs the pipeline with the recorded rule pack, depth and target
- Diffs the canonical payload against the recorded one; exits non-zero on any difference
- Refuses with a clear message if `rule_pack_hash` or `engine_version` no longer match
- Allowed roles: `auditor`, `reviewer`
- `ubo eval` gains the replay-fidelity metric (target 100 %)

---

## Step 9 — End-to-End Tests (Fixtures)
**Verifiable:** `make test` green; coverage ≥ 85 % on `rules/` and `graph/`; 100 % gold scenarios; all PRD §10 metrics at target on fixtures.

### 9.1 Integration tests (`tests/integration/`)
- `test_screen_pipeline.py` — for each gold scenario: `Supervisor.run(query)` → assert recommendation, expected paths in `memo_json`, expected gaps, zero uncited claims, hash chain verifies
- `test_determinism.py` — each scenario screened twice in a fresh database → identical canonical payload and identical `decision_id`
- `test_replay.py` — screen, then replay → identical; tamper with one log line → `audit verify` fails
- `test_review_state_machine.py` — non-reviewer rejected; override without reason rejected; second review rejected
- `test_roles.py` — analyst memo contains no person PII; auditor cannot write; engineer cannot review
- `test_latency.py` — p95 across scenarios < 5 s
- Suite runs with networking disabled (socket guard fixture)

### 9.2 Coverage gate — in `make test` only
```make
test:
	uv run pytest --cov=src/ubo_sentinel/rules --cov=src/ubo_sentinel/graph --cov-fail-under=85
```
Not in `addopts`, so the single-file commands in earlier steps are not failed by the gate.

---

## Step 10 — Real Data Loaders
**Verifiable:** `make refresh` downloads and ingests; `uv run ubo eval --suite er` reports ER precision@1 on real names; `uv run ubo bench` reports p95 < 15 s; every entry in `eval/real_smoke.yaml` returns its expected recommendation.

Downloading (`make refresh` → `var/downloads/`) is separate from ingesting (`ubo ingest --source gleif --path var/downloads/…`). After download, nothing needs the network.

### 10.1 GLEIF Level 1 loader (`pipeline/loaders/gleif_lei_loader.py`)
- Resolves the latest publish via `goldencopy.gleif.org/api/v2/golden-copies/publishes/latest`; downloads the **CSV** golden copy
- Loads with DuckDB `read_csv` straight into bronze
- Captures registration authority id + number and other/transliterated names (aliases)
- `--limit N` for quick dev cycles; relationship rows whose endpoints were cut are dropped and counted

### 10.2 GLEIF Level 2 loader (`pipeline/loaders/gleif_rr_loader.py`)
- RR-CDF CSV; `IS_DIRECTLY_CONSOLIDATED_BY` → `CONSOLIDATED_BY` edge with `pct=None`, `basis="accounting_consolidation"`
- `IS_ULTIMATELY_CONSOLIDATED_BY` → same with `is_ultimate=True`
- Only active relationship records become edges

### 10.3 GLEIF Reporting Exceptions loader (`pipeline/loaders/gleif_repex_loader.py`)
- Maps exception reason (`NATURAL_PERSONS`, `NON_CONSOLIDATING`, `NON_PUBLIC`, …) to `repex_reason`

### 10.4 OpenSanctions FTM loader (`pipeline/loaders/opensanctions_loader.py`)
- Reads `entities.ftm.json` (NDJSON)
- `LegalEntity` / `Company` / `Organization` / `Person` → `bronze_entities` (with `leiCode`, registration numbers, aliases)
- `Sanction` → `bronze_sanctions` (program, dates, active flag)
- `Ownership` → `OWNS` edge. `percentage` is free text: a parser handles `"50"`, `"50%"`, `"50.0"`; ranges, inequalities and unparseable values become `pct = None` plus a data gap
- `Directorship` → `CONTROLS` edge
- Cross-source join to GLEIF happens in Silver entity linking (4.2): `leiCode` → registration number → fuzzy name + jurisdiction

### 10.5 Real-data checks
- `eval/er_labelled.csv` extended with hand-labelled real names; P3 exit: ER precision measured and reported
- `eval/real_smoke.yaml` — a fixed list of real LEIs with expected recommendations, chosen and verified by hand against the downloaded snapshot at build time (kept by LEI, never by name alone)
- `ubo bench --lei-file …` — screens the list, reports p50/p95; NFR target p95 < 15 s on a 16 GB laptop

---

## Step 11 — Streamlit Review UI + pyvis Graph
**Verifiable:** `streamlit run src/ubo_sentinel/ui/app.py` opens in browser; the demo script in `RUNBOOK.md` (written in this step) runs end-to-end.

### 11.1 Review UI (`src/ubo_sentinel/ui/app.py`)
- Sidebar: entity search field + role selector (all four roles)
- Main: runs the supervisor on submit; shows a candidate picker when it returns `NEEDS_DISAMBIGUATION`; shows the memo for the selected role
- Decision panel: Approve / Override (reason required) / Escalate — reviewer only; uses the same state-machine code as `ubo review`
- Decision history table (last 20, from the audit log); auditor gets a "Replay" button

### 11.2 pyvis evidence graph (`src/ubo_sentinel/ui/graph_viz.py`)
- `render_subgraph(view: GraphView, rule_result) -> HTML` — reads through the role view so masking applies
- Colours: blocked (`B`) = red, possibly blocked (`P`) = amber, other = green; target outlined
- Edge labels: rel_type + pct % ("?" for null-%)
- Embedded in Streamlit via `st.components.v1.html`

### 11.3 `RUNBOOK.md`
Step-by-step demo: ingest fixtures → screen each headline scenario → review → audit verify → replay → list change → re-screen queue.

---

## Step 12 — Snapshot Diff & Re-screen Queue (G6)
**Verifiable:** `ubo ingest --source fixtures --path fixtures/snapshot_t1` then `ubo monitor --prev <set_1> --curr <set_2>` prints the change digest and re-screen queue; `uv run pytest tests/integration/test_change_monitor.py` asserts the exact queue for the fixture snapshots.

### 12.1 Snapshot differ (`src/ubo_sentinel/pipeline/differ.py`)
- `diff_snapshots(set_a, set_b)` → `SnapshotDiff(added, removed, changed)` on designations
- Changed = any field change on a `Sanction` record, including `is_active` flipping

### 12.2 Reverse traversal + re-screen queue (`agents/change_monitor.py`)
- Added/changed: `queries.get_affected_entities(node_id, max_depth)` on the **current** graph
- Removed: the same on the **previous** snapshot's graph (the node or its links may no longer exist in the current one)
- Intersect with the latest decision per target from the audit log
- Write `var/rescreen_queue.jsonl` with `{entity_id, reason, triggered_by, prev_decision_id}`, sorted deterministically

### 12.3 `ubo monitor` CLI command
- Change digest for the reviewer: N designations added / removed / changed, and which prior decisions each touches
- Re-screen queue size
- `--autorun` executes the re-screens; each produces a new `RECOMMENDED` decision that still needs a reviewer

---

## Step 13 (Stretch) — LLM Assists, Neo4j Export, UK/EU Packs
**Verifiable:** with every flag on, `ubo eval --suite all` results are unchanged; `uv run pytest tests/unit/test_llm_guardrail.py tests/unit/test_match_adjudicator.py tests/unit/test_intake_assist.py` passes offline.

LLM assists are added in this order: memo prose (13.1), review-band match adjudication (13.4), intake query understanding (13.5). Traversal, Rule Engine, Guardrail, Audit and the Supervisor stay as code. 13.4 and 13.5 go beyond PRD v1.0 (FR-17 covers memo prose only) and need the amendments listed at the end.

### 13.1 LLM adapter (`agents/llm_polish.py`)
- Runs only when `UBO_LLM=on` in env
- Sends the validated `memo_json` only (never raw graph data) to Anthropic `claude-haiku-4-5-20251001`
- Receives prose; Guardrail re-validates that every number/name exists in `memo_json`
- On any mismatch: discard LLM output, use the template memo, **and record a guardrail warning** in the decision
- Test injects tampered prose through a stub client and asserts the fallback — no live model needed
- The recommendation, paths and canonical payload are never affected by the LLM; the prose is stored under `Decision.llm_annotations`

### 13.2 Neo4j export adapter (`pipeline/exporters/neo4j_export.py`)
- `ubo export --target neo4j --uri bolt://localhost:7687` (behind `UBO_NEO4J=on`)

### 13.3 Illustrative UK/EU packs (`rules/uk_ofsi.yaml`, `rules/eu.yaml`)
- Expressed purely through rule-pack config (`threshold_operator: ">"`, `aggregate_blocked_owners`, control test)
- Each file and each memo produced with them is labelled "illustrative — not validated by counsel"
- Own small gold-scenario file; the OFAC gold scenarios must be unaffected

### 13.4 Review-band match adjudication (`agents/match_adjudicator.py`)
- Runs only when `UBO_LLM_MATCH=on`, after the Rule Engine Agent, and only for sanction matches in `[review_band_low, match_threshold)`
- Input per match: the two records side by side (names, aliases, jurisdiction, registration ids, dates, addresses), passed through `GraphView(role)` first
- Output: `MatchAssessment(match_id, assessment: Literal["LIKELY_SAME","LIKELY_DIFFERENT","INSUFFICIENT"], rationale, evidence_fields[])`
- **Advisory only.** It does not change `match_confidence`, the blocked or possibly-blocked sets, or the recommendation: the match stays in the review band and the decision stays `REVIEW`. The reviewer sees the assessment next to the match, labelled "LLM-assisted".
- Guardrail: the rationale may refer only to fields present in the two input records; a deterministic check drops any assessment that names a value not in its input and logs a warning
- Stored under `Decision.llm_annotations` and in the audit log; replay reads the stored text
- Eval: `eval/match_adjudication_labelled.csv` (record pairs labelled same/different); `ubo eval --suite match` reports agreement with the labels, run offline against recorded responses

### 13.5 Intake query understanding (`agents/intake_assist.py`)
- Runs only when `UBO_LLM_INTAKE=on`, and only when the deterministic resolver returned zero candidates or `NEEDS_DISAMBIGUATION`; never when an exact identifier matched
- Input: the raw query string only. Output: `QueryInterpretation(name_variants[], jurisdiction_hint, entity_type_hint)` — transliterations, legal-form expansions, descriptors stripped
- The deterministic resolver (5.4) is re-run with the variants. Candidates come only from the resolver; the LLM cannot introduce an entity.
- **A candidate found through an LLM variant is never auto-selected.** It always goes to the analyst (HITL #1), whatever its score.
- Optional one-line explanation of how the candidates differ, built from their fields and checked by the same "only values present in the input" guardrail
- The interpretation is stored in the audit log; the decision records the chosen `target_id`, so replay and determinism are unaffected
- Eval: `eval/er_labelled.csv` extended with messy queries; `ubo eval --suite er` reports recall@5 with and without the assist, and confirms precision@1 does not drop

---

## Step 14 (Extension) — LLM Investigator, beside the Supervisor
**Not in PRD v1.0** — requires the amendments listed in "PRD Amendments Required for Step 14" below. Rough effort: 7–9 days.

**Verifiable:** `uv run pytest tests/unit/test_investigator_guardrail.py tests/integration/test_investigator.py` passes offline against recorded model responses; with `UBO_INVESTIGATOR=on`, `ubo investigate <decision_id> "Who else does this owner control?"` returns cited findings and `ubo audit replay-investigation <investigation_id>` prints "Replay identical"; with the flag on, `ubo eval --suite all` is unchanged.

**Design rule:** the deterministic Supervisor still produces every decision. The Investigator is a separate, optional entry point that answers a question about an existing decision. It can read, and it can ask for another screen; it can never change a recommendation, a decision's status, or the canonical decision payload.

### 14.1 Extra relationship data and queries
With only the Step 5 tools, "dig deeper" returns the graph the pipeline already walked. This sub-step gives the Investigator something new to find.
- Silver: `silver_addresses(entity_id, address_norm, country, + provenance columns)` from GLEIF legal/headquarters address and OpenSanctions `Address`; officer links already arrive as `CONTROLS` edges (10.4)
- Five additions to the allow-list in `graph/queries.py` (eight → thirteen typed functions; still no free-form queries):
```python
def get_entities_sharing_address(entity_id: str, snapshot_set_id: str) -> list[EntityMatch]
def get_common_controllers(entity_ids: list[str], snapshot_set_id: str) -> list[OwnerEdge]
def get_sibling_entities(entity_id: str, snapshot_set_id: str) -> list[Entity]
def find_paths_between(source_id: str, target_id: str, max_depth: int, snapshot_set_id: str) -> list[Path]
def get_entity_history(entity_id: str, snapshot_set_ids: list[str]) -> list[EntityChange]
```
- Fixtures: add shared-address and common-director cases to `fixtures/snapshot_t0/`

### 14.2 Tool layer (`src/ubo_sentinel/agents/investigator_tools.py`)
- Tool definitions generated from the Pydantic signatures of the thirteen query functions, plus `run_screen(target_id, max_depth)`
- Graph tools are read-only. `run_screen` goes through the deterministic Supervisor, which logs a new `RECOMMENDED` decision as usual
- Every tool result passes through `GraphView(role)` before it reaches the model, so masked person data never leaves the machine for an `analyst`
- No tool exists for review transitions or for writing to the audit log

### 14.3 Orchestrator loop (`src/ubo_sentinel/agents/investigator.py`)
- Runs only when `UBO_INVESTIGATOR=on` and an API key is present; otherwise the command exits with a clear message
- Anthropic SDK tool-use loop; model set by `UBO_INVESTIGATOR_MODEL` (needs a stronger model than the Haiku used for memo polish)
- Hard budgets from config: max steps, max tokens, wall-clock limit; on exhaustion it returns what it has, marked incomplete
- Input: `decision_id`, the question, the role. Context given to the model: the decision's validated `memo_json` only
- Entity names, aliases and addresses come from external data and are passed as tool-result data, never placed in the instructions
- Output: `Investigation(investigation_id, decision_id, question, role, findings[], suggested_actions[], steps[], model, budgets, complete: bool)`; each finding lists the tool-call ids it rests on

### 14.4 Investigation record and replay
- New audit event types `INVESTIGATION_STARTED`, `INVESTIGATION_STEP`, `INVESTIGATION_FINISHED` (extends `AuditEvent.event_type` from 1.1), appended to the same hash chain; they never alter a decision's status
- Each step event stores the tool name, arguments, full result and the model output
- `ubo audit replay-investigation <investigation_id>` re-executes the recorded tool calls against the recorded snapshot set and confirms each result matches; it reads the stored model outputs and does not call the model
- Investigations are excluded from the canonical decision payload, so decision determinism and replay fidelity are unaffected

### 14.5 Investigator guardrail (`agents/investigator_guardrail.py`)
- Deterministic check: every entity name, id, percentage and date in a finding must appear in a tool result that the finding cites; a finding that fails is dropped and a warning is logged
- `suggested_actions` is limited to an enum: `RESCREEN_DEEPER`, `SCREEN_RELATED_ENTITY`, `REQUEST_DOCUMENT`, `SUGGEST_ESCALATION` — advisory text for the reviewer, never applied automatically
- Findings are stored and displayed separately from the memo and labelled "LLM-assisted, advisory"

### 14.6 Evaluation (`eval/investigation_scenarios.yaml`)
- Each entry: `decision scenario`, `question`, `expected_findings`, `must_not_claim`
- CI runs offline against recorded model responses (`tests/recordings/`); a live run is opt-in
- `ubo eval --suite investigator` reports: citation validity (target 100 %), finding precision, finding recall, mean steps, mean cost per investigation

### 14.7 Interface
- `ubo investigate <decision_id> "<question>"` (`cli/investigate_cmd.py`); allowed roles `analyst`, `reviewer`; prints findings plus the tool trace
- Streamlit: a chat panel in the decision view showing findings, citations and the tool trace; `auditor` sees past investigations read-only with a "Replay" button

---

## Step 15 (Later phase) — Ownership Extraction from Documents
**Not in PRD v1.0 and not scheduled.** Outline only; it addresses the main data weakness (GLEIF carries no ownership percentages). Depends on Steps 3–4 and the LLM conventions. Rough effort: 5–7 days.

**Verifiable:** `uv run pytest tests/unit/test_extraction_guardrail.py tests/integration/test_document_extraction.py` passes offline; an accepted edge appears in a new snapshot and changes a screen result only through the normal deterministic pipeline.

### 15.1 Extraction (`pipeline/extraction/document_extractor.py`)
- `ubo extract <file> --entity <id>` behind `UBO_LLM_EXTRACT=on`; text extracted locally from PDF or plain text (adds a PDF text dependency such as `pypdf`)
- The LLM proposes `ProposedEdge(owner_name, asset_name, rel_type, pct, quote, page, confidence)`
- Proposals are written to `var/extraction_queue.jsonl`; nothing reaches the graph at this point

### 15.2 Extraction guardrail
- The `quote` must appear verbatim in the document text on the cited page; the percentage must appear inside the quote
- Owner and asset names are resolved by the deterministic resolver; unresolved names are flagged, never auto-created
- A proposal that fails either check is dropped with a logged warning

### 15.3 Human confirmation and ingest
- `ubo extract review` (role `reviewer`): accept, edit or reject each proposal; every action is an audit event
- Accepted edges are ingested to Bronze as source `document:<sha256 of file>` with `source_record_id` = file hash + page, forming a new content-derived snapshot
- From there the edge flows through Silver reconciliation (4.2) and Gold like any other source, so screening stays deterministic and fully cited

### 15.4 Evaluation
- A small labelled document set in `eval/extraction/`; metrics: verbatim-quote validity (target 100 %), precision of accepted-quality proposals, recall of stated holdings

---

## Critical Files Summary

| Area | Files |
|---|---|
| Models | `src/ubo_sentinel/models/{provenance,entity,relationship,sanction,decision,audit_event,ontology}.py` |
| Pipeline | `src/ubo_sentinel/pipeline/{bronze,silver,gold,transformer,entity_linking,differ}.py` |
| Loaders | `src/ubo_sentinel/pipeline/loaders/{fixture,gleif_lei,gleif_rr,gleif_repex,opensanctions}_loader.py` |
| Graph | `src/ubo_sentinel/graph/{projection,queries,entity_resolution,views}.py` |
| Rules | `src/ubo_sentinel/rules/engine.py` · `rules/ofac.yaml` |
| Agents | `src/ubo_sentinel/agents/{base,supervisor,intake,traversal,sanctions_match,rule_engine,explainer,guardrail,audit,change_monitor}.py` |
| LLM assists (Step 13) | `src/ubo_sentinel/agents/{llm_polish,match_adjudicator,intake_assist}.py` · `eval/match_adjudication_labelled.csv` · `tests/unit/test_{llm_guardrail,match_adjudicator,intake_assist}.py` |
| Document extraction (Step 15) | `src/ubo_sentinel/pipeline/extraction/document_extractor.py` · `cli/extract_cmd.py` · `eval/extraction/` |
| Investigator (Step 14) | `src/ubo_sentinel/agents/{investigator,investigator_tools,investigator_guardrail}.py` · `cli/investigate_cmd.py` · `eval/investigation_scenarios.yaml` · `tests/recordings/` · `tests/unit/test_investigator_guardrail.py` · `tests/integration/test_investigator.py` |
| Audit | `src/ubo_sentinel/audit/{log,replay}.py` |
| CLI | `src/ubo_sentinel/cli/app.py` · `cli/{ingest,normalize,build_graph,eval,screen,review,audit,bench,monitor}_cmd.py` |
| UI | `src/ubo_sentinel/ui/{app,graph_viz}.py` |
| Tests | `tests/unit/test_{models,ontology,fixtures,bronze,silver,graph,views,rules,rules_hypothesis,llm_guardrail}.py` · `tests/integration/test_{screen_pipeline,determinism,replay,review_state_machine,roles,latency,change_monitor}.py` |
| Data | `fixtures/snapshot_t0/*.csv` · `fixtures/snapshot_t1/sanctions.csv` · `eval/{gold_scenarios.yaml,er_labelled.csv,real_smoke.yaml}` · `ontology/ontology.yaml` |
| Skills | `.claude/skills/{build-step,add-data-source,add-rule-pack,add-gold-scenario}/SKILL.md` |
| Docs | `README.md` · `CLAUDE.md` · `ONTOLOGY.md` · `AGENT_CONTRACTS.md` · `BUILD_PLAN.md` · `RUNBOOK.md` |

---

## Verification Summary (per step)

| Step | PRD phase | Verification command |
|---|---|---|
| 0 — Skeleton + skills | P0 | `uv run python -c "import ubo_sentinel; print('ok')"` · `uv run ubo --help` · four skills listed in Claude Code |
| 1 — Models + ontology | P0 | `uv run pytest tests/unit/test_models.py tests/unit/test_ontology.py -v` |
| 2 — Fixtures | P0 | `uv run pytest tests/unit/test_fixtures.py -v` |
| 3 — Bronze | P0 | `uv run ubo ingest --source fixtures` (row counts; idempotent) |
| 4 — Silver | P0 | `uv run ubo normalize` · `uv run pytest tests/unit/test_silver.py -v` |
| 5 — Gold + queries + views | P1 | `uv run ubo build-graph` · `uv run pytest tests/unit/test_graph.py tests/unit/test_views.py -v` |
| 6 — Rules | P1 | `uv run ubo eval --suite rules` → 100 % · `pytest tests/unit/test_rules*.py` |
| 7 — Agents + screen | P2 | `uv run ubo screen "Acme Trading FZE" --snapshot fixtures` → CLEAR in < 5 s, identical on re-run |
| 8 — Review, audit, replay | P2 | `ubo review` · `ubo audit verify` · `ubo audit replay <id>` |
| 9 — Integration | P2 | `make test` green; ≥ 85 % coverage; all §10 metrics at target on fixtures |
| 10 — Real data | P3 | `make refresh` · `ubo eval --suite er` · `ubo bench` p95 < 15 s · `eval/real_smoke.yaml` passes |
| 11 — Streamlit | P4 | `streamlit run src/ubo_sentinel/ui/app.py`; `RUNBOOK.md` demo runs end-to-end |
| 12 — Re-screen | P4 | `ubo monitor --prev <s1> --curr <s2>` · `pytest tests/integration/test_change_monitor.py` |
| 13 — Stretch | P5 | Flags on: `ubo eval --suite all` unchanged · `pytest tests/unit/test_{llm_guardrail,match_adjudicator,intake_assist}.py` · `ubo eval --suite match` |
| 15 — Document extraction | new (P7, unscheduled) | `pytest tests/unit/test_extraction_guardrail.py tests/integration/test_document_extraction.py` |
| 14 — Investigator | new (P6) | `pytest tests/unit/test_investigator_guardrail.py tests/integration/test_investigator.py` · `ubo investigate <id> "…"` · `ubo audit replay-investigation <id>` · `ubo eval --suite investigator` |

## PRD Traceability

| PRD item | Delivered in | Verified by |
|---|---|---|
| FR-01 GLEIF L1 / L2 / RepEx ingest | 10.1–10.3 | `make refresh`; bronze row counts |
| FR-02 OpenSanctions ingest | 10.4 | `make refresh`; bronze row counts |
| FR-03 Fixtures, default mode | 2, 3.2 | `test_fixtures.py`, `ubo ingest --source fixtures` |
| FR-04 Silver per ontology | 1.2, 4 | `test_ontology.py`, `test_silver.py` |
| FR-05 Gold graph + provenance | 5.1–5.2 | `test_graph.py` |
| FR-06 Entity resolution | 4.2, 5.4 | `ubo eval --suite er` |
| FR-07 Traversal, depth, cycles | 5.2, 7.3 | `test_graph.py`, SCEN-15 |
| FR-08 OFAC fix-point | 6.2 | `ubo eval --suite rules`, Hypothesis tests |
| FR-09 Effective exposure | 6.2 step 4 | SCEN-08, SCEN-09, SCEN-10 |
| FR-10 Data-gap handling | 6.2 step 5 | SCEN-04, SCEN-12, SCEN-13, SCEN-14 |
| FR-11 Memo (MD + JSON) with citations | 7.6–7.7 | `test_screen_pipeline.py` (0 uncited) |
| FR-12 Reviewer capture (CLI + UI) | 8.1, 11.1 | `test_review_state_machine.py`, RUNBOOK |
| FR-13 Hash-chained log + replay | 7.8, 8.2, 8.3 | `test_replay.py`, `ubo audit verify` |
| FR-14 Role-scoped views | 5.5 | `test_views.py`, `test_roles.py` |
| FR-15 Snapshot diff + re-screen | 12 | `test_change_monitor.py` |
| FR-16 Evidence graph visual | 11.2 | RUNBOOK demo |
| FR-17 LLM polish | 13.1 | `test_llm_guardrail.py` |
| FR-18 Neo4j export | 13.2 | manual, behind flag |
| NFR Latency (fixtures / full GLEIF) | 5.2, 9.1, 10.5 | `test_latency.py`, `ubo bench` |
| NFR Determinism | conventions, 1.1, 3.1 | `test_determinism.py` |
| NFR Keyless / offline | conventions, 10 | socket guard in test suite |
| NFR Auditability | 7.8, 8.3 | `test_replay.py` |
| NFR Coverage | 9.2 | `make test` |
| Metric: gold accuracy 100 % | 6.4 | `ubo eval` |
| Metric: path recall ≥ 98 % | 6.4 | `ubo eval` |
| Metric: uncited claims 0 | 7.7 | `ubo eval` |
| Metric: ER precision@1 ≥ 95 % | 2.3, 4.2, 10.5 | `ubo eval --suite er` |
| Metric: replay fidelity 100 % | 8.3 | `ubo eval`, `test_replay.py` |
| §11 Human authority state machine | 8.1 | `test_review_state_machine.py` |
| §11 Allow-listed queries only | 5.3 | code review; agents import only `graph/queries.py` |
| §3 Non-goal: UK/EU not authoritative | 13.3 | labelled illustrative in file and memo |
| FR-21 Review-band match adjudication (proposed) | 13.4 | `test_match_adjudicator.py`, `ubo eval --suite match` |
| FR-22 Intake query understanding (proposed) | 13.5 | `test_intake_assist.py`, `ubo eval --suite er` |
| FR-23 Document extraction (proposed, post-MVP) | 15 | `test_extraction_guardrail.py`, `test_document_extraction.py` |
| FR-19 LLM investigator (proposed) | 14.2–14.7 | `test_investigator.py`, `ubo eval --suite investigator` |
| FR-20 Extended relationship queries (proposed) | 14.1 | `test_graph.py` additions |

## Build Order Rationale

Steps 0–6 are strictly sequential (each depends on the previous layer); the CLI app exists from Step 0 and each step registers the command it verifies with. Steps 7–8 (agents, review, audit, replay) follow Step 6. Step 9 caps the fixture-based MVP and maps to the PRD's P2 exit. Step 10 (real data), Step 11 (UI) and Step 12 (re-screen) are independent of each other and can proceed in parallel after Step 9. Step 13 is stretch, behind flags. Within Step 13 the LLM assists are built in the order 13.1 → 13.4 → 13.5. Step 14 follows: it depends on the query layer (Step 5), the audit log (Steps 7–8) and the review UI (Step 11), needs the PRD amendments below, and changes nothing in Steps 0–12. Step 15 is a later, unscheduled phase that depends only on Steps 3–4 and the LLM conventions.

## PRD Amendments Required for Step 14

To be applied to `ubo-sentinel-PRD.md` by the owner before Step 14 is built. Numeric targets marked *(proposed)* are suggestions to confirm.

| PRD section | Amendment |
|---|---|
| §3 Goals | Add **G7 — Assisted investigation (optional):** a reviewer or analyst can ask a follow-up question about a decision and receive cited, advisory findings. The investigator never produces or alters a decision. |
| §3 Non-goals | Clarify "No free-text LLM-generated graph queries in the hot path": the investigator calls typed allow-listed functions only and runs outside the screening path. State explicitly whether adverse-media screening remains a non-goal; if it does, the investigator is limited to graph and snapshot data. |
| §4 Personas | `analyst` and `reviewer` may run investigations (analyst sees masked person data); `auditor` may view and replay them; `engineer` may not. |
| §5 Workflows | Add a third workflow: *decision → question → investigator calls allow-listed tools → guardrail validates citations → advisory findings shown to reviewer → optional deterministic re-screen*. |
| §6 Functional requirements | Add **FR-19** — LLM investigator over allow-listed tools, behind `UBO_INVESTIGATOR=on`, advisory only, full transcript in the audit log (P2). Add **FR-20** — extended relationship queries: shared address, common controllers, siblings, paths between two parties, entity history across snapshots (P2). |
| §7 NFR — Keyless | Restate as: the screening core (FR-01 to FR-16) runs with zero API keys and no network after data download. FR-17 and FR-19 require a key and are off by default. |
| §7 NFR — Determinism | Restate as applying to the canonical decision payload. Investigations are not deterministic but are replayable from the stored transcript. |
| §7 NFR — Latency | Add an investigator budget: ≤ 12 tool steps and ≤ 90 s per question *(proposed)*. |
| §10 Success metrics | Add: finding citation validity 100 %; finding precision on `eval/investigation_scenarios.yaml` ≥ 90 % *(proposed)*; gold-scenario results unchanged with the flag on. |
| §11 Guardrails | Extend "No free-form queries": allow-list grows from eight to thirteen functions. Extend "LLM isolation": the investigator receives only role-masked tool results; text from external data is treated as data, not instructions; it has no tool that changes decision state; every step is logged. Extend "Human authority": investigator output is advisory and cannot transition a decision. |
| §12 Architecture | Add `Investigator (LLM, optional)` beside the Supervisor in the agent plane, calling the same typed query API. Stack: Anthropic SDK now used for memo polish and the investigator. |
| §13 Release plan | Add **P6 Investigator** (7–9 days): exit criteria — offline investigator tests pass, investigation replay identical, `ubo eval` unchanged with the flag on. |
| §14 Risks | Add: prompt injection via entity names/aliases (mitigation: read-only tools, data passed as tool results, citation guardrail); entity data sent to an external API (mitigation: role masking before send, off by default, documented); cost and latency (mitigation: hard budgets); over-reliance on advisory findings (mitigation: labelled, separate from memo, no decision rights). |
| §15 Open questions | Add: Is sending unmasked legal-entity data to an external model API acceptable for the demo? Should the investigator be able to use sources outside the graph (registry documents, adverse media)? |

## PRD Amendments Required for Steps 13.4, 13.5 and 15

To be applied to `ubo-sentinel-PRD.md` by the owner. FR-17 (memo prose) is unchanged. Numeric targets marked *(proposed)* are suggestions to confirm.

| PRD section | Amendment |
|---|---|
| §5 Primary workflow | Note two optional, flagged assists: at ① the Intake Agent may use an LLM to generate search variants; after ④ an LLM may annotate review-band matches. Neither selects an entity nor changes the recommendation. |
| §6 Functional requirements | Add **FR-21** — LLM adjudication notes on review-band sanctions matches, behind `UBO_LLM_MATCH=on`, advisory only (P2). Add **FR-22** — LLM query understanding for intake, behind `UBO_LLM_INTAKE=on`; candidates come only from the resolver and are never auto-selected (P2). Add **FR-23** — ownership extraction from analyst-supplied documents with verbatim-quote validation and human confirmation, behind `UBO_LLM_EXTRACT=on` (post-MVP). |
| §8 Data sources | Add a row for **analyst-supplied documents** (annual reports, registry filings): proposed `OWNS` edges with percentage, entering Bronze only after human confirmation; licence: the user's own documents. *(Step 15 only.)* |
| §9 Decision logic | State that LLM adjudication does not alter the seed, the match thresholds or the blocked/possibly-blocked sets; a review-band match yields `REVIEW` regardless of the LLM's assessment. |
| §10 Success metrics | Add: adjudication agreement with labelled pairs ≥ 90 % *(proposed)*; intake recall@5 improves with the assist while precision@1 does not drop; extracted-edge verbatim-quote validity 100 %. |
| §11 Guardrails — LLM isolation | Generalise from "receives only the validated memo JSON" to a per-feature input list: memo JSON (prose), two role-masked records (adjudication), the raw query string (intake), document text (extraction). In every case the output is advisory, is checked by code against its input, is stored in the audit log, and cannot change a decision; entities enter only through the resolver and edges only through human confirmation. |
| §13 Release plan | P5 Stretch: add match adjudication and intake assist. Add **P7 Document extraction** (5–7 days, unscheduled): exit criteria — offline extraction tests pass; accepted edges appear as a new snapshot. |
| §14 Risks | Add: an LLM assessment may anchor the reviewer (mitigation: labelled, shows the evidence fields, no effect on the recommendation); a wrongly extracted edge (mitigation: verbatim-quote check plus human confirmation); confidential documents sent to an external API (mitigation: off by default, documented). |
| §15 Open questions | Add: Which role may confirm extracted edges (assumed: reviewer)? May client documents be sent to an external model API? |
