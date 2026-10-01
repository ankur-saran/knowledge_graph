---
name: add-rule-pack
description: Add a new ubo-sentinel rule pack (for example a UK OFSI or EU ownership-and-control test) as YAML configuration, with its own gold scenarios, and confirm the OFAC gold scenarios are unchanged. Use when asked to add or draft a rule pack or a new jurisdiction's rule.
---

# Add a rule pack

**Input:** a rule-pack id (`$ARGUMENTS`, for example `uk_ofsi`) and the rule it should express: ownership threshold, comparison operator, whether blocked owners' stakes are aggregated, and how control is treated. If the rule is not specified, ask and stop.

Read `CLAUDE.md` (conventions and `## Project facts`) and Step 6 and Step 13.3 of `BUILD_PLAN.md` first. Requires `rules/ofac.yaml`, `src/ubo_sentinel/rules/engine.py` and `ubo eval` to exist (Step 6); if they do not, say so and stop.

## Procedure

1. Run `uv run ubo eval --suite rules` and keep the output as the baseline.
2. Create `rules/<id>.yaml` by copying `rules/ofac.yaml` and changing values only.
   - Use **only keys that already exist** in `rules/ofac.yaml`. No engine changes. If the rule cannot be expressed with the existing keys, report which behaviour is missing and stop; a new key is an engine change and needs its own build step.
   - Set a new `id`.
   - Unless the pack has been validated by compliance counsel, put this comment on the first line: `# illustrative — not validated by counsel`.
3. Create `eval/gold_scenarios_<id>.yaml` in the same format as `eval/gold_scenarios.yaml`, with at least: one case on each side of the threshold, one exactly at the threshold (this is where `>` and `>=` differ), one aggregation case, and one control-only case. Reuse existing fixture entities where they fit; if new fixture rows are needed, use the `add-gold-scenario` skill first and stop here.
4. Confirm the pack is selectable through `--rule-pack <id>` on `ubo screen` and `ubo eval`. If the CLI keeps a list of accepted ids, add the new id there; that is the only Python change this skill makes.
5. Run the evaluator on the new pack and on OFAC.

## Done

- Every scenario in `eval/gold_scenarios_<id>.yaml` passes with the new pack.
- `uv run ubo eval --suite rules` for OFAC is identical to the baseline from step 1.
- `uv run pytest` passes.

## Report

- The new files and the keys whose values differ from `rules/ofac.yaml`
- Actual evaluator output for the new pack and for OFAC, before and after
- Whether the pack is labelled illustrative
