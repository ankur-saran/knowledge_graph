# PRD — ubo-sentinel

**Knowledge-Graph-driven Beneficial Ownership & Sanctions Exposure Screening with AI Agents**

| Field | Value |
|---|---|
| Owner | Ankur Saran · AI Product Leader \| AI First Builder |
| Status | Draft v1.0 — ready for Claude Code build |
| Date | 2026-09-30 |
| Build mode | Local-first, keyless by default, deterministic core, human-in-the-loop |
| Companion docs | `ONTOLOGY.md`, `AGENT_CONTRACTS.md`, `BUILD_PLAN.md`, `RUNBOOK.md`, `CLAUDE.md` |

---

## 1. Problem statement

When a firm onboards a counterparty — or when a sanctions list changes — a KYC analyst must answer one question:

> **Is this entity owned or controlled, directly or indirectly, by one or more sanctioned parties — and can I prove it?**

Today this is a manual, multi-tab exercise: look up the entity, walk its parent chain in a registry, cross-check each owner against several sanctions lists, add up ownership percentages by hand, and write a memo. It is slow (hours per complex case), inconsistent between analysts, and hard to audit.

The problem is fundamentally **relational**. The risk hides 2–5 hops up an ownership tree, and the regulatory test (e.g. OFAC's 50 Percent Rule) is an **aggregation over paths**. Vector search / RAG cannot compute this; flat SQL can only with brittle recursive queries. A knowledge graph with purpose-built agents can.

## 2. Why a Knowledge Graph (and not RAG)

| Need | RAG / vector | Relational SQL | **Knowledge Graph + agents** |
|---|---|---|---|
| Walk an ownership chain of unknown depth | ✗ | Recursive CTE, brittle | ✓ native traversal |
| Aggregate % held by *multiple* blocked owners (OFAC rule) | ✗ | Hard | ✓ fix-point over graph |
| Explain the decision with evidence | Chunk citations only | Row dumps | ✓ **path-as-evidence** |
| Provenance per fact (source, as-of, confidence) | Partial | Possible | ✓ edge properties |
| React to list changes (re-screen impacted entities) | ✗ | Hard | ✓ reverse traversal from changed node |

**Positioning line:** *"The graph computes; the agents orchestrate, explain and ask a human."*

## 3. Goals and non-goals

### Goals (MVP)

1. **G1 — Screen on demand:** Given an entity name or LEI, return a decision recommendation (`CLEAR` / `REVIEW` / `ESCALATE`) in < 5 s on a laptop, with every contributing ownership path.
2. **G2 — Rule-correct aggregation:** Implement OFAC 50 Percent Rule semantics (aggregate, indirect via blocked intermediates) exactly; pass 100 % of the gold scenarios in `eval/gold_scenarios.yaml`.
3. **G3 — Explainability:** Produce a risk memo in which every factual claim cites a graph path + source record + as-of date. Zero uncited claims.
4. **G4 — Human-in-the-loop:** No agent can finalize a decision. A reviewer approves, overrides (with reason) or escalates; all actions are logged.
5. **G5 — Governance:** Provenance on every node/edge, role-scoped graph views, allow-listed queries only, tamper-evident audit log.
6. **G6 — Event-driven re-screen:** When the sanctions snapshot changes, identify every previously screened entity whose decision could change, and queue it.

### Non-goals (MVP)

- Not a production sanctions-screening system; not legal advice. Rule packs must be validated by compliance counsel before any real use.
- No transaction screening, payment filtering or adverse-media screening.
- No free-text LLM-generated graph queries in the hot path.
- No multi-tenant auth server; roles are simulated locally.
- No UK/EU rule packs as *authoritative* — shipped as configurable examples only.

## 4. Users and personas

| Persona | Job to be done | Graph view (role) |
|---|---|---|
| **KYC Analyst** (primary) | Screen a new counterparty; get a defensible recommendation fast | `analyst` — full ownership graph, masked person PII |
| **Compliance Reviewer / MLRO** | Approve, override or escalate; sign off | `reviewer` — full graph incl. person details |
| **Auditor** | Re-play any past decision exactly as it was made | `auditor` — read-only, time-travel to snapshot |
| **Platform Engineer** | Refresh data, run evals, monitor drift | `engineer` — pipelines, no decision rights |

## 5. Primary workflow (happy path)

```
[Analyst] "Screen Acme Trading FZE"
   │
   ▼
① Intake Agent ──► resolves name → candidate LEIs (fuzzy + registry match)
   │                 └─ ambiguous? → ask analyst to pick (HITL #1)
   ▼
② Traversal Agent ─► extracts ownership subgraph (up to N hops, both directions for context)
   ▼
③ Sanctions Match Agent ─► matches every node in subgraph to sanctions snapshot
   ▼
④ Rule Engine Agent ─► applies rule pack (OFAC default): fix-point aggregation,
   │                     effective exposure, data-gap flags → recommendation
   ▼
⑤ Explainer Agent ─► builds risk memo; every claim → path + source + as-of
   ▼
⑥ Guardrail Agent ─► validates memo (no uncited claims, role masking, schema) 
   ▼
⑦ Reviewer (HITL #2) ─► APPROVE / OVERRIDE(reason) / ESCALATE
   ▼
⑧ Audit Agent ─► writes hash-chained decision record + snapshot pointers
```

### Secondary workflow — list change re-screen (G6)

`Sanctions snapshot t → t+1` ⇒ diff added/removed/changed designations ⇒ **reverse traversal** (who does the changed node own, down N hops) ⇒ intersect with previously screened entities ⇒ enqueue re-screens ⇒ notify reviewer with a change digest.

## 6. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Ingest GLEIF Level 1 (LEI-CDF), Level 2 relationships (RR-CDF) and Reporting Exceptions into Bronze | P0 |
| FR-02 | Ingest OpenSanctions (FollowTheMoney `entities.ftm.json`) sanctions collection into Bronze | P0 |
| FR-03 | Ingest synthetic fixtures (`fixtures/`) for offline demo + tests; fixture mode is default | P0 |
| FR-04 | Normalize into Silver canonical tables conforming to `ontology/ontology.yaml` | P0 |
| FR-05 | Build Gold graph (nodes/edges tables + NetworkX projection) with provenance on every element | P0 |
| FR-06 | Entity resolution: LEI exact match > registration number > fuzzy name + jurisdiction; confidence score | P0 |
| FR-07 | Ownership traversal with configurable max depth (default 5) and cycle detection | P0 |
| FR-08 | OFAC rule pack: aggregate ≥ 50 % via fix-point; indirect only through blocked intermediates | P0 |
| FR-09 | Effective (multiplicative) exposure as an *additional risk signal* (not the legal test) | P1 |
| FR-10 | Data-gap handling: consolidation edges without %, reporting exceptions, lapsed LEIs → `REVIEW` with reason | P0 |
| FR-11 | Risk memo (Markdown + JSON) with path citations | P0 |
| FR-12 | Reviewer decision capture (CLI + Streamlit UI) | P0 |
| FR-13 | Hash-chained append-only audit log; decision replay by `decision_id` | P0 |
| FR-14 | Role-scoped views: PII masking for `analyst`, read-only for `auditor` | P1 |
| FR-15 | Snapshot diff + re-screen queue | P1 |
| FR-16 | Graph visual of the evidence subgraph (pyvis HTML) | P1 |
| FR-17 | Optional LLM narrative polish for memo (behind `UBO_LLM=on`), never alters facts | P2 |
| FR-18 | Optional Neo4j export adapter | P2 |

## 7. Non-functional requirements

| Area | Target |
|---|---|
| Latency | Screen p95 < 5 s on fixtures, < 15 s on full GLEIF graph (laptop, 16 GB) |
| Determinism | Same inputs + same snapshot ⇒ byte-identical decision JSON (excluding timestamps) |
| Keyless | Full MVP runs with zero API keys and no network after data download |
| Auditability | Every decision replayable from `decision_id` → snapshot ids → paths |
| Test coverage | ≥ 85 % on `rules/` and `graph/`; 100 % gold-scenario pass |
| Data freshness | GLEIF & OpenSanctions both publish daily; demo refresh is manual `make refresh` |

## 8. Data sources

| Source | What it gives | Format / access | License note |
|---|---|---|---|
| **GLEIF Golden Copy — Level 1 (LEI-CDF)** | Who is who: legal name, jurisdiction, status, registration authority ids | Daily CSV/JSON/XML zip; latest-publish API `goldencopy.gleif.org/api/v2/golden-copies/publishes/latest` | GLEIF data is free and open (verify current LEI Data Terms of Use) |
| **GLEIF Level 2 (RR-CDF)** | Who owns whom: `IS_DIRECTLY_CONSOLIDATED_BY`, `IS_ULTIMATELY_CONSOLIDATED_BY`, fund/branch relations; status; validation sources | Same API, `rr` key | as above |
| **GLEIF Reporting Exceptions** | Why a parent is *not* reported (e.g. `NATURAL_PERSONS`, `NON_CONSOLIDATING`, `NON_PUBLIC`) — a **data-gap signal** | Same API, `repex` key | as above |
| **OpenSanctions — sanctions collection** | Designated persons/companies, `Sanction` records, `Ownership` edges with `percentage`, `leiCode` on legal entities | `entities.ftm.json` (daily) | **CC BY-NC 4.0** — free for non-commercial; commercial use needs a license |
| **Synthetic fixtures** | Deterministic scenarios incl. OFAC FAQ-style aggregation cases | `fixtures/*.csv`, `eval/gold_scenarios.yaml` | Own |

**Key data reality (design-driving):** GLEIF Level 2 describes *accounting consolidation*, not percentage ownership — quantifiers are optional and usually empty. Therefore:

- Consolidation edges are modelled as `CONSOLIDATED_BY` with `pct = null` and `basis = accounting_consolidation`.
- Percent ownership comes from OpenSanctions `Ownership` records and fixtures (`OWNS` edges with `pct`).
- The rule pack decides how to treat unknown % (`presume_majority_for_consolidation: true` in OFAC pack ⇒ outcome `REVIEW`, never silent `CLEAR`).

## 9. Decision logic (OFAC pack — default)

1. **Seed** the blocked set `B₀` = entities in the subgraph matched to an active designation with match confidence ≥ `match_threshold` (default 0.92) — or exact identifier match.
2. **Fix-point:** repeat until no change — for each entity *e* not in *B*, compute `agg(e) = Σ pct(o → e)` over direct owners *o ∈ B*. If `agg(e) ≥ 50`, add *e* to *B* (reason: `DERIVED_50PCT`, with contributing edges).
   - This is how OFAC FAQ 401 treats *indirect* ownership: shares held through an entity count only if that intermediate is itself ≥ 50 % owned by blocked persons (and then count in full).
3. **Effective exposure** (risk signal only): `max over paths Π pct` and `Σ over paths Π pct` from any blocked node to the target.
4. **Data gaps:** consolidation edge with null %, reporting exception, lapsed/retired LEI, match confidence in `[review_band_low, match_threshold)`.
5. **Recommendation:**
   - `ESCALATE` — target ∈ *B* (designated or derived-blocked).
   - `REVIEW` — target ∉ *B* but any of: effective exposure ≥ `review_exposure_pct` (default 25), data gap on a path to a blocked/possibly-blocked node, sub-threshold aggregate ≥ `near_miss_pct` (default 40), control-only link to a blocked party.
   - `CLEAR` — none of the above; memo still lists the traversal depth and sources checked.

> The engine **recommends**; only a human reviewer decides. UK/EU packs (`rules/uk_ofsi.yaml`, `rules/eu.yaml`) are illustrative (e.g. "more than 50 %", control test, different aggregation) and must be validated by counsel.

## 10. Success metrics

| Metric | Target (MVP) |
|---|---|
| Gold-scenario decision accuracy | 100 % |
| Evidence path recall (expected paths present in memo) | ≥ 98 % |
| Uncited claims in memos | 0 |
| Entity-resolution precision @ top-1 on labelled set | ≥ 95 % |
| Analyst time per complex case (demo narrative) | hours → < 5 minutes |
| Replay fidelity (decision JSON identical on replay) | 100 % |

## 11. Guardrails & governance

- **No free-form queries:** agents call typed functions in `graph/queries.py` only (allow-list). A Text2Cypher mode is out of scope.
- **Provenance everywhere:** every node/edge carries `source`, `source_record_id`, `snapshot_id`, `as_of`, `confidence`.
- **Role-scoped views:** a `GraphView(role)` wrapper masks `Person` PII for `analyst`, denies writes for `auditor`.
- **LLM isolation:** if enabled, the LLM receives only the validated memo JSON and returns prose; Guardrail Agent re-verifies that every number/name in the prose exists in the JSON. On any mismatch, fall back to the template memo.
- **Audit:** JSONL log; each record contains `prev_hash`, `hash = sha256(prev_hash + canonical_json(record))`. `ubo audit verify` checks the chain.
- **Human authority:** decision states `RECOMMENDED → APPROVED | OVERRIDDEN | ESCALATED`; only role `reviewer` can transition.

## 12. Architecture (logical)

```
          ┌────────────────────── Data plane (deterministic) ──────────────────────┐
 GLEIF ─► │ Bronze (raw, immutable, per snapshot) ─► Silver (canonical, ontology) ─► │
 OpenSan ─►│ Gold (graph_nodes, graph_edges, provenance) ─► NetworkX projection     │
 Fixtures►└──────────────────────────────────────────────────────────────────────────┘
                                         │ typed query API (allow-list)
          ┌──────────────────────── Agent plane ─────────────────────────────────────┐
          │ Supervisor (state machine)                                               │
          │  Intake → Traversal → SanctionsMatch → RuleEngine → Explainer → Guardrail│
          │  → HumanReview (HITL) → Audit          + ChangeMonitor (event-driven)     │
          └──────────────────────────────────────────────────────────────────────────┘
                     │                               │
               CLI (typer)                    Streamlit review UI + pyvis graph
```

Stack: Python 3.12 · uv · DuckDB (medallion) · NetworkX · Pydantic v2 · rapidfuzz · typer · Streamlit · pyvis · pytest/hypothesis. Optional: Anthropic SDK (memo polish), Neo4j driver (export).

## 13. Release plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Foundations** (days 1–2) | Repo, ontology, contracts, fixtures, DuckDB medallion on fixtures | `make test` green; ontology validator passes |
| **P1 Graph + Rules** (days 3–5) | Gold graph, traversal, OFAC fix-point, effective exposure, gap flags | 100 % gold scenarios on rule engine alone |
| **P2 Agents + HITL** (days 6–8) | All agents, supervisor, memo, guardrail, audit, CLI | End-to-end screen on fixtures < 5 s; audit verify passes |
| **P3 Real data** (days 9–10) | GLEIF + OpenSanctions loaders, entity resolution on real names | Screen real LEIs; ER precision measured |
| **P4 UX + events** (days 11–13) | Streamlit review UI, pyvis evidence graph, snapshot diff re-screen | Demo script in `RUNBOOK.md` runs end-to-end |
| **P5 Stretch** | LLM polish, Neo4j export, UK/EU packs | Behind flags; evals unchanged |

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| GLEIF lacks % ownership | Explicit null-% semantics → `REVIEW`; % from OpenSanctions/fixtures |
| Name-matching false positives | Identifier-first ER; review band; analyst disambiguation step |
| OpenSanctions licence (NC) | Demo/personal use only; document; swap to licensed feed for commercial |
| Rule mis-implementation | Gold scenarios modelled on OFAC FAQ examples; property-based tests |
| LLM hallucination | LLM optional, post-validated, template fallback |
| Graph size on laptop | Subgraph extraction per screen; DuckDB for storage, NetworkX only for the ego-graph |

## 15. Open questions

1. Default traversal depth — 5 hops enough for demo? (Assumed yes.)
2. Include `Person` UBOs from OpenSanctions `Ownership` in MVP, or entities only? (Assumed: include, masked for analysts.)
3. Should `REVIEW` auto-escalate after SLA breach? (Out of MVP.)
