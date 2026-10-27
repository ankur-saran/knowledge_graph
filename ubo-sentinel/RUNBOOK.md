# Runbook

A demo of `ubo-sentinel` on the synthetic fixtures, about 15 minutes. Each step gives the review UI and the command line side by side: the UI calls the same code, under the same permission matrix, and writes to the same audit log.

Run every command from `ubo-sentinel/`. Nothing here needs the network except the optional step 7.

> Not a production sanctions-screening system; not legal advice; rule packs must be validated by compliance counsel.

## 1. Build the fixture graph

```
uv sync --all-extras
uv run ubo ingest --source fixtures
uv run ubo normalize
uv run ubo build-graph
```

Each command prints its snapshot or set id and its row counts. Run again, each says `already present` or `already built`: ids come from file contents, so the same files give the same ids on any machine.

To start the demo again from nothing, delete `var/`.

## 2. Start the UI

```
uv run ubo ui
```

Streamlit serves the UI on `http://localhost:8501`, to this machine only, and sends no usage statistics (`.streamlit/config.toml`). The graph library is inline in the page: nothing is fetched from the network.

In the sidebar, choose a **role** and an **actor**. Both are simulated: choosing them is not authentication. Each role sees the panels of the commands the ontology lets it run:

| Role | Panels | Sees persons |
|---|---|---|
| analyst | Screen | masked: `[PII MASKED]`, `person:…` pseudonyms |
| reviewer | Screen, Review, Audit | yes |
| auditor | Audit (verify, replay, read-only decisions) | yes |
| engineer | Audit (verify only), Build info | no decisions, metadata only |

## 3. Screen as an analyst

Role `analyst`, actor `a.analyst`. Screen each query in the Screen tab, or with `uv run ubo screen "<query>" --actor a.analyst`.

| Query | Expect | What to show |
|---|---|---|
| Acme Trading FZE | `CLEAR` | The graph shows everything checked; the memo says how far the screen went and what it read. |
| Vostrek Maritime LLC | `ESCALATE` (`DIRECT_DESIGNATION`) | The target is a red diamond: designated by its LEI. |
| Halcyon Ridge Minerals Ltd | `ESCALATE` (`DERIVED_50PCT`) | Two designated owners at 30 % each. Their names are masked for the analyst; switch to `reviewer` to see them. |
| Fairweather Bulk Carriers Ltd | `REVIEW` | A 60 % owner matched to a list record by name only, in the review band: a dashed outline, and *What would settle this review* says `ESCALATE` if confirmed, `CLEAR` if rejected. |
| Silverline Commodities Ltd | asks which one | Two homonyms. Pick 1 (`CLEAR`), then screen again and pick 2 (`ESCALATE`): the pick decides the answer, so it is recorded. |
| Pendlewick Salt Refiners Ltd | `REVIEW` (`POSSIBLE_DUPLICATE`) | A linked list record has an owner the screened graph does not show. *Next actions* offers a screen of the linked record: press it, and the record is `ESCALATE` (its 80 % owner is designated). |
| Kittiwake Packaging Ltd | `REVIEW` (`DEPTH_LIMIT_REACHED`) | The owner at depth 5 has owners above it. *Next actions* offers a screen 7 hops deep, which finds the designated owner: `ESCALATE`. |

Things to point out on any decision:

- **Already on record.** Screening a query again says `already on record, unchanged` and records nothing. The decision is computed again and compared with the recorded one, so every repeat is also a check that nothing changed.
- **The audit head** under each decision is a receipt. Keep one for step 5.
- **The evidence graph** is drawn from the decision's memo, not the live database, so it shows the evidence the rules read. *Evidence behind a node* lists the claims about a node and opens the published record each one cites (a masked stub for a masked person).
- *The graph as a table* and the *Legend* say the same as the drawing, without relying on colour.

## 4. Review as a reviewer

Role `reviewer`. The Review tab lists the decisions waiting, `ESCALATE` first, then the oldest. Or: `uv run ubo review --role reviewer`.

1. **Maker and checker.** Keep the actor `a.analyst` and open the Vostrek decision: the review is refused, because the person who screened it cannot review it. Change the actor to `r.reviewer`.
2. **Approve an `ESCALATE`.** Vostrek: *approve*, then *Record the review* → `APPROVED, outcome ESCALATE`. Or: `uv run ubo review <id> --role reviewer --actor r.reviewer --action approve`.
3. **Settle a match.** Fairweather: choose *It is the listed party → ESCALATE*, give a reason, and record it. The reviewer settles the fact and the rules give the outcome. An outcome that contradicts the ruling is refused. Or: `--action approve --confirm-matches --reason "…"`.
4. **What cannot be cleared.** A query that matched nothing (screen `Nobody Ltd`) is `REVIEW` with `ENTITY_NOT_FOUND`: the outcome `CLEAR` is not offered, because nothing was screened.
5. **A second review** of the same decision is refused, in the UI and on the command line (exit code 2).

## 5. Verify and replay as an auditor

Role `auditor`, Audit tab. Or the commands shown.

1. **Verify:** paste the receipt from step 3 into *Expected head* and press *Verify* → `Chain intact (N records)`. Or: `uv run ubo audit verify --expect-head <hash>`. A log cut short after the receipt was taken no longer holds that head.
2. **Replay:** choose a decision and press *Replay* → `Replay identical`. Or: `uv run ubo audit replay <id>`. The recorded request is screened again, rule pack from the event, against the recorded snapshot set, after checking each snapshot's row digest and the graph digest. Tick *--deep* to recompute the digests from the rows themselves.
3. The auditor sees each decision read-only, with *How this was computed*: per agent, its time and its queries.

## 6. A list changes

`fixtures/snapshot_t1/` holds the sanctions list with one designation added (Acme's majority owner), one removed and one made inactive.

```
uv run ubo ingest --source fixtures --path fixtures/snapshot_t1
uv run ubo normalize
uv run ubo build-graph
```

`latest` now names the t1 set; `fixtures` still names t0.

1. In the sidebar set the snapshot set to `latest`, and as `a.analyst` screen `Acme Trading FZE` (now `ESCALATE`), `Nettlebed Cider Company Ltd` (now `CLEAR`) and `Quenby Aerostructures Ltd` (now `ESCALATE`).
2. As `r.reviewer`, open Acme's t0 decision (the `CLEAR`). It is marked *newer data exists*, and *Other decisions on this entity* shows the t1 `ESCALATE` beside it. Approving it as `CLEAR` is refused; ticking *Clear on the older data all the same* with a reason allows it, and the review event records that it was accepted as stale.

The re-screen queue, which finds these decisions without screening each entity again, is Step 12 and is added to this runbook with it.

## 7. Real data (optional, needs the network)

```
uv run ubo refresh
```

`refresh` downloads GLEIF and the OpenSanctions sanctions collection (about 1 GB), then builds and checks them; it takes about half an hour. Then, with the snapshot set `real`, screen by LEI `253400WSS48YWMBUA688` (Gazprombank: `ESCALATE`, `DIRECT_DESIGNATION`) and `HWUPKR0MPOU8FGXBT394` (Apple Inc.: `CLEAR`). `eval/real_smoke.yaml` lists more, each with the recommendation it was verified with. The UI names the OpenSanctions licence (CC BY-NC 4.0) under every decision that read it.

## Troubleshooting

| Message | Do |
|---|---|
| `Snapshot set … is unknown. Run 'ubo ingest' first.` | Step 1. |
| `Graph not built …` | Run the command it names (`ubo normalize`, `ubo build-graph`). |
| `The audit log does not verify` | Nothing is shown or recorded on a broken chain. `ubo audit verify` names the first line that fails. |
| The UI cannot read the ontology | Start it with `uv run ubo ui` from `ubo-sentinel/`, or set `UBO_ONTOLOGY`. |
| A database lock error during `ubo ingest` | The UI holds no connection while idle; wait for a screen in progress to finish. |
