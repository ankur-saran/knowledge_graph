# Agent contracts

The screening pipeline is seven agents run in a fixed order by the Supervisor (`src/ubo_sentinel/agents/supervisor.py`). There is no model in it: an agent is a class with a `run(state, ctx)` method, and the Supervisor is a loop.

`Intake → Traversal → SanctionsMatch → RuleEngine → Explainer → Guardrail → Audit`

Human review (`RECOMMENDED → APPROVED | OVERRIDDEN | ESCALATED`) is a later command, not a step of a screen (Step 8 of [BUILD_PLAN.md](BUILD_PLAN.md)).

## The contract

Each agent declares the fields of `PipelineState` it `requires` and the fields it `provides`. The Supervisor checks the first before `run` and the second after it, and raises `ContractError` naming the agent. `tests/unit/test_agent_contracts.py` asserts that the table below equals the declarations in the code.

| Agent | Module | Requires | Provides | Runs after a halt |
|---|---|---|---|---|
| Intake | `agents/intake.py` | — | `resolution`, `target_id` | no |
| Traversal | `agents/traversal.py` | `target_id` | `subgraph` | no |
| SanctionsMatch | `agents/sanctions_match.py` | `subgraph` | `sanction_matches` | no |
| RuleEngine | `agents/rule_engine.py` | `subgraph`, `sanction_matches` | `rule_result` | no |
| Explainer | `agents/explainer.py` | — | `memo` | yes |
| Guardrail | `agents/guardrail.py` | `memo` | `guardrail_warnings` | yes |
| Audit | `agents/audit.py` | `resolution`, `memo`, `guardrail_warnings` | `decision` | yes |

An agent may also set `halt`. The agents between it and the Explainer are then skipped, and what it `provides` is not checked:

| `halt` | Set by | What happens |
|---|---|---|
| `NEEDS_DISAMBIGUATION` | Intake | The screen stops and returns the candidates. The caller runs it again with `pick` or `target_id`. Nothing is recorded. |
| `ENTITY_NOT_FOUND` | Intake | A decision `REVIEW / ENTITY_NOT_FOUND` with no target. |
| `EVIDENCE_LIMIT_EXCEEDED` | RuleEngine | A decision `REVIEW / EVIDENCE_LIMIT_EXCEEDED`: the graph has more evidence paths than the rule pack's `max_paths`. |

## Inputs and outputs

| Type | Where | What it is |
|---|---|---|
| `ScreenRequest` | `agents/base.py` | What the caller typed: `query`, `lei`, `registration_number`, `jurisdiction`, `target_id`, `pick`, `snapshot`, `rule_pack`, `max_depth`, `role`, `actor`. None of it is in the canonical payload. |
| `ScreenContext` | `agents/base.py` | One open `GraphStore` (so one snapshot set), the rule pack and engine, the ontology, the clock, the audit log, the trace. |
| `PipelineState` | `agents/base.py` | `request`, `resolution`, `target_id`, `subgraph`, `sanction_matches`, `rule_result`, `what_if`, `memo`, `guardrail_warnings`, `decision`, `halt`. Frozen: an agent returns a copy. |
| `Resolution` | `agents/base.py` | How the query became the target: `method`, `target_id`, the candidates shown, the linked records folded into each. |
| `Memo` | `models/memo.py` | The memo: an entity table, an edge table, and claims, paths and matches that refer to them by id. Every claim has citations. |
| `Decision` | `models/decision.py` | The decision of record. `canonical_payload()` is what determinism and replay compare. |
| `DecisionView` | `models/memo.py` | A decision as one role sees it. Made by `graph/views.py` `mask_decision()`. |

`Supervisor.run(request)` returns one of:

| Outcome | Meaning |
|---|---|
| `NeedsDisambiguation` | Several entities answer to the query. |
| `Recommended` | A decision in status `RECOMMENDED`. `is_new` is false when the same inputs were screened before and the recorded decision is returned. |
| `ScreenFailed` | An agent raised. Shown as `REVIEW / PIPELINE_ERROR`; a `SCREEN_FAILED` event is logged; no decision is recorded. |

It raises, and records nothing, for what is not a fault of the screen: `UnknownRulePack`, `UnknownSnapshotSet`, `GraphNotBuilt`, `ScreenUsageError` (an unknown `target_id`, a `pick` out of range), `PermissionError` (a read-only role), `AuditChainError`, `DeterminismViolation`.

## What each agent does

**Intake.** Calls `resolve_entity`. Candidates that are linked to each other at or above the rule pack's `match_threshold` are one choice, represented by the registry's record. It selects a target alone only for one identifier match, or one name at or above `match_threshold`; weaker candidates are kept in the `Resolution`. Otherwise it halts with `NEEDS_DISAMBIGUATION`.

**Traversal.** Calls `get_ownership_subgraph` with the request's `max_depth`, else the rule pack's.

**SanctionsMatch.** Calls `get_sanctioned_nodes`. Classification is the rule engine's.

**RuleEngine.** Calls `RuleEngine.evaluate`. For a `REVIEW` that rests on an unsettled match it also runs the rules with those matches confirmed and with them rejected (`what_if`). That changes nothing in the result; it tells the reviewer what the review decides.

**Explainer.** `build_memo()` is a pure function of the subgraph, the matches and the rule result. `render_memo()` turns a memo into Markdown from `templates/memo.md.j2`. The stored memo is unmasked.

**Guardrail.** Two guardrails, kept apart:

- The *decision guardrail* (`check_memo`) reads the memo, the rule result and the store. It checks that the memo survives a JSON round trip, that every claim is cited, that every id is described, that the memo agrees with the rules, that every citation leads to a record of the screened set, and that the rendered text states only values the memo holds. A fault is a warning in the decision, adds the reason `GUARDRAIL_FAILED`, and raises the recommendation to at least `REVIEW`. It never lowers an `ESCALATE`.
- The *render guardrail* (`check_render`) reads what one role is about to be shown. On a fault the output is not shown. It cannot change a decision, so an analyst and a reviewer get the same one.

**Audit.** Builds the `Decision` and appends a `RECOMMENDED` event whose payload holds the decision, its payload hash, the rule pack's values, the request, the resolution and the trace. If the `decision_id` is already on record, the new decision is compared with the recorded one: equal, the recorded one is returned and nothing is written; different, `DeterminismViolation` is raised.

## Rules that hold across agents

- Agents read graph data only through `graph/queries.py`, and read the store unmasked.
- A decision is a function of its inputs. Anything that changes what the agents conclude raises `PIPELINE_VERSION` in `agents/base.py`, which is part of every `decision_id`.
- A failure never produces a `CLEAR`.
