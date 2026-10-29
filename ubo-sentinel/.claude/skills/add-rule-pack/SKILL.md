---
name: add-rule-pack
description: Add a new ubo-sentinel rule pack (for example a UK OFSI or EU ownership-and-control test) as YAML configuration, with its expectations in the pack scenarios, and confirm the OFAC gold scenarios are unchanged. Use when asked to add or draft a rule pack or a new jurisdiction's rule.
---

# Add a rule pack

**Input:** a rule-pack id (`$ARGUMENTS`, for example `uk_ofsi`) and the rule it should express: ownership threshold, comparison operator, whether blocked owners' stakes are aggregated, and how control is treated. If the rule is not specified, ask and stop.

Read `CLAUDE.md` (conventions and `## Project facts`) and Step 6 and Step 13.3 of `BUILD_PLAN.md` first. Requires `rules/ofac.yaml`, `src/ubo_sentinel/rules/engine.py` and `ubo eval` to exist (Step 6); if they do not, say so and stop.

## Procedure

1. Run `uv run ubo eval --suite rules` and keep the output as the baseline.
2. Create `rules/<id>.yaml` by copying `rules/ofac.yaml` and changing values only. The file stem is the name `--rule-pack` takes; there is no list of accepted ids.
   - Use **only keys that already exist** in `rules/ofac.yaml`. No engine changes. If part of the rule cannot be expressed with the existing keys (control that blocks, for one: the engine only sends a control link to review), choose the nearest cautious setting, say what is missing in a comment and in the pack's `label`, and report it. If the core ownership test itself cannot be expressed, report what is missing and stop: a new key is an engine change (`RULES_VERSION`) and needs its own build step.
   - Set a new `id`.
   - Unless the pack has been validated by compliance counsel, put this comment on the first line: `# illustrative — not validated by counsel`, and set `label:` to a sentence saying so (the memo prints it). `label` is the one optional key; the OFAC pack has none.
   - Name the lists it applies in `designation_lists`: a fixture stand-in (`FX-...`) and the OpenSanctions dataset id, checked against the dataset's page.
3. Add the new pack's expectation to **every** entry of `eval/pack_scenarios.yaml` (inline graphs, no fixtures; schema in `src/ubo_sentinel/rules/pack_scenarios.py`); the suite fails while any entry does not state it. Add entries where the new rule differs from the others: each side of the threshold, exactly at it (`>` against `>=`), an aggregation case, a control-only case. Do not add fixture rows for a pack: they would move every snapshot, decision id and golden decision.
4. Run `uv run ubo eval --suite packs`, then the evaluator on OFAC.

## Done

- `uv run ubo eval --suite packs` reports Pack agreement 100 %, with the new pack in the matrix.
- `uv run ubo eval --suite rules` for OFAC is identical to the baseline from step 1.
- `uv run pytest` passes.

## Report

- The new files and the keys whose values differ from `rules/ofac.yaml`
- The pack matrix, and the OFAC evaluator output before and after
- What the rule needs that the engine cannot express
- Whether the pack is labelled illustrative
