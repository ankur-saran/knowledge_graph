---
name: add-gold-scenario
description: Add one ubo-sentinel gold scenario - the synthetic fixture rows for it, the gold entry with expected recommendation, reasons, sets, paths and gaps - then run the fixture-integrity test and the evaluator. Use when asked to add a test scenario, an edge case, or a SCEN-NN.
---

# Add a gold scenario

**Input:** a description of the ownership situation and the expected recommendation (`CLEAR`, `REVIEW` or `ESCALATE`) (`$ARGUMENTS`). If the expected recommendation is not given, work it out from Step 6.2 of `BUILD_PLAN.md`, state the reasoning, and ask for confirmation before writing anything.

Read `CLAUDE.md` (conventions and `## Project facts`), Step 2 and Step 6.2 of `BUILD_PLAN.md`, `ONTOLOGY.md` and `fixtures/README.md` first. Requires `fixtures/snapshot_t0/*.csv`, `eval/gold_scenarios.yaml` and `tests/unit/test_fixtures.py` to exist (Step 2); if they do not, say so and stop.

## Procedure

1. Pick the next free id `SCEN-NN` in `eval/gold_scenarios.yaml`. Ids are contiguous.
2. Design the scenario so that one rule decides it, and say which. If it is a variation of an existing scenario that differs by one fact and has another outcome, it is that scenario's twin.
3. Add fixture rows to `fixtures/snapshot_t0/`, using the columns in `fixtures/README.md`:
   - `entities.csv` — new, synthetic entities only, with `source_record_id` starting `sNN-`. Do not edit or reuse entities that belong to another scenario; a shared owner changes that scenario's result.
   - Give each entity a distinctive invented name. A name that resembles another entity's fails the calibration test unless the gold entry declares the pair.
   - `relationships.csv` — edges in the ontology's direction: `OWNS` owner → asset, `CONSOLIDATED_BY` child → parent, `CONTROLS` controller → controlled.
   - `sanctions.csv`, `repex.csv` — only if the scenario needs them. Do not touch `fixtures/snapshot_t1/` unless the scenario is about the changed list; if a `sanctions.csv` row is added, add the same row there.
   - Keep the files UTF-8 without BOM with LF line endings. Append rows; follow the existing id pattern.
4. Add the entry to `eval/gold_scenarios.yaml` (schema: `GoldScenario` in `src/ubo_sentinel/models/gold_scenario.py`): `id`, `query`, `target_id`, `expected_recommendation`, `rationale`, and whichever of these apply: `suite`, `expect_disambiguation`, `twin_of`, `expected_recommendation_t1`, `expected_reasons`, `expected_blocked`, `expected_possibly_blocked`, `expected_exposure`, `expected_paths`, `expected_gaps`, `declared_name_pairs`.
   - Node ids are canonical: `lei:<LEI>` when the entity has an LEI, otherwise `<source>:<source_record_id>`.
   - A path lists `nodes` from the blocked party to the target and `via`, the relation of each hop. Never write an edge id.
   - The rationale names the Step 6.2 rule that produces the recommendation.
5. Run `uv run pytest tests/unit/test_fixtures.py -v`.
6. Run `uv run ubo eval --suite rules` if the evaluator exists (Step 6). If it does not yet exist, say that the scenario is unevaluated.

## Done

- The fixture-integrity test passes.
- The evaluator passes the new scenario, and every scenario that passed before still passes.

If the engine returns a different result from the expected one, do not change the engine or the expectation to make it pass. Report both, with the engine's reason codes, and stop.

## Report

- The scenario id, the rows added to each CSV, and the gold entry
- Actual output of the fixture-integrity test and the evaluator
