---
name: add-gold-scenario
description: Add one ubo-sentinel gold scenario - the synthetic fixture rows for it, the gold entry with expected recommendation, paths and gaps - then run the fixture-integrity test and the evaluator. Use when asked to add a test scenario, an edge case, or a SCEN-NN.
---

# Add a gold scenario

**Input:** a description of the ownership situation and the expected recommendation (`CLEAR`, `REVIEW` or `ESCALATE`) (`$ARGUMENTS`). If the expected recommendation is not given, work it out from Step 6.2 of `BUILD_PLAN.md`, state the reasoning, and ask for confirmation before writing anything.

Read `CLAUDE.md` (conventions and `## Project facts`), Step 2 and Step 6.2 of `BUILD_PLAN.md`, and `ONTOLOGY.md` first. Requires `fixtures/snapshot_t0/*.csv`, `eval/gold_scenarios.yaml` and `tests/unit/test_fixtures.py` to exist (Step 2); if they do not, say so and stop.

## Procedure

1. Pick the next free id `SCEN-NN` in `eval/gold_scenarios.yaml`.
2. Add fixture rows to `fixtures/snapshot_t0/`:
   - `entities.csv` — new, synthetic entities only. Do not edit or reuse entities that belong to another scenario; a shared owner changes that scenario's result.
   - `relationships.csv` — edges in the ontology's direction: `OWNS` owner → asset, `CONSOLIDATED_BY` child → parent, `CONTROLS` controller → controlled.
   - `sanctions.csv`, `repex.csv` — only if the scenario needs them.
   - Keep ids and row order deterministic: append rows, follow the existing id pattern.
3. Add the entry to `eval/gold_scenarios.yaml` with `id`, `query_entity`, `pick` (only if the name is ambiguous), `expected_recommendation`, `expected_paths`, `expected_gaps`, `rationale`. The rationale names the Step 6.2 rule that produces the recommendation.
4. Run `uv run pytest tests/unit/test_fixtures.py -v`.
5. Run `uv run ubo eval --suite rules` if the evaluator exists (Step 6). If it does not yet exist, say that the scenario is unevaluated.

## Done

- The fixture-integrity test passes.
- The evaluator passes the new scenario, and every scenario that passed before still passes.

If the engine returns a different recommendation from the expected one, do not change the engine or the expectation to make it pass. Report both, with the engine's reason codes, and stop.

## Report

- The scenario id, the rows added to each CSV, and the gold entry
- Actual output of the fixture-integrity test and the evaluator
