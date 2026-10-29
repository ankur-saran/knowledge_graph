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

1. **Find what the change may move, without screening every entity again:**

   ```
   uv run ubo monitor
   ```

   It diffs t1 against the set each recorded decision was made on and prints the change digest (one designation added, one removed, one changed, each with the decisions it reaches), then the re-screen queue. Of the entities screened in step 3, the change reaches Acme alone: its `CLEAR` is queued. (Screen `Quenby Aerostructures Ltd` and `Nettlebed Cider Company Ltd` on `fixtures` before loading t1 and they are queued too, a cleared entity first.) The run is recorded once in the audit log (`MONITOR_RUN`), with a receipt; run it again and it says *already on record*. `--dry-run` records nothing (the only mode for `auditor`); `--json` prints the run.
2. **Screen the queue again:** `uv run ubo monitor --autorun --role analyst`. Each re-screen is an ordinary decision by `system:monitor` on t1: Acme `CLEAR -> ESCALATE (UPGRADE)` (Quenby `CLEAR -> ESCALATE`, Nettlebed `REVIEW -> CLEAR (DOWNGRADE)`, if they were screened). Nothing is cleared by the monitor: each waits for a reviewer. In the UI the reviewer's Review tab shows the same queue under *Re-screen queue* and re-screens one item at a time.
3. As `r.reviewer`, the review queue marks each re-screen *re-screen of <decision>*. Open Acme's t0 decision (the `CLEAR`): it is marked *newer data exists*, and *Other decisions on this entity* shows the t1 `ESCALATE` beside it. Approving it as `CLEAR` is refused; ticking *Clear on the older data all the same* with a reason allows it, and the review event records that it was accepted as stale.

## 6b. The graph changes

`fixtures/snapshot_t2/` changes what a list diff cannot see: a stake (Marrowdale's designated owner now holds 50 % of the intermediate), an LEI a list record starts to state (so Fairweather's owner and the designated list record are one entity), and an LEI that lapsed (Silverline's parent).

```
uv run ubo ingest --source fixtures --path fixtures/snapshot_t2
uv run ubo normalize
uv run ubo build-graph
uv run ubo monitor --autorun --role analyst
```

The digest names each change by cause (`OWNERSHIP`, `LINK`, `ENTITY`). Of the entities screened in step 3, Fairweather (approved in step 4, so queued last: its `ESCALATE` can only stay or fall) comes back `ESCALATE` with no match to settle, and Silverline (pick 1) stays `CLEAR`, with the lapsed LEI as an informational gap. (Screen `Marrowdale Dairy Ltd` with the snapshot set `latest` before loading t2, and it is queued too and comes back `ESCALATE`.) A queued decision whose outcome does not move costs a re-screen; a missed one would be an entity dealt with on old data. `uv run ubo eval --suite monitor` screens every gold scenario on `fixtures`, queues them against the newest set, screens all of them again and reports **Re-screen recall** (target 100 %) and precision.

## 7. Real data (optional, needs the network)

```
uv run ubo refresh
```

`refresh` downloads GLEIF and the OpenSanctions sanctions collection (about 1 GB), then builds and checks them; it takes about half an hour. Then, with the snapshot set `real`, screen by LEI `253400WSS48YWMBUA688` (Gazprombank: `ESCALATE`, `DIRECT_DESIGNATION`) and `HWUPKR0MPOU8FGXBT394` (Apple Inc.: `CLEAR`). `eval/real_smoke.yaml` lists more, each with the recommendation it was verified with. The UI names the OpenSanctions licence (CC BY-NC 4.0) under every decision that read it.

## 8. Other regimes, Neo4j and LLM assists (optional)

The UK and EU packs are illustrative, not validated by counsel, and send control to review instead of treating it as designation. One graph, every pack:

```
uv run ubo eval --suite packs
uv run ubo screen "Fenwold Turbine Services Ltd" --rule-pack eu
```

Under OFAC, Fenwold is `REVIEW` (`OTHER_REGIME_DESIGNATION`): its owner is on the EU list only. Under the EU pack the same designation blocks, and the memo carries the pack's label.

`uv run ubo export neo4j --snapshot fixtures` writes CSVs, `import.cypher` and a manifest under `var/export/<set id>/` (persons masked for an analyst; `--role reviewer` for names). Copy the CSVs into Neo4j's import directory and run `cypher-shell -f import.cypher`, or load a server directly with `--uri bolt://localhost:7687` and the password in `NEO4J_PASSWORD`. Nothing reads Neo4j back.

The LLM assists need `uv sync --extra optional`, an Anthropic API key, `UBO_LLM=on` and the feature's flag. Each answer is advisory, recorded in the audit log, and changes no decision.

```
uv run ubo annotate <decision id> --feature match --role reviewer   # the match cards: no flag needed
UBO_LLM=on UBO_LLM_PROSE=on uv run ubo annotate <decision id> --feature prose
UBO_LLM=on UBO_LLM_INTAKE=on uv run ubo screen "Halcyon Ridge"
```

The model reads the analyst's view only. A summary names parties through slots that code fills, so a reviewer reads names where an analyst reads pseudonyms. The intake assist lists the candidates its search variants found by id: screen one with the `--target-id … --assist-run …` it prints.

## Troubleshooting

| Message | Do |
|---|---|
| `Snapshot set … is unknown. Run 'ubo ingest' first.` | Step 1. |
| `Graph not built …` | Run the command it names (`ubo normalize`, `ubo build-graph`). |
| `ubo monitor`: `… built by other code` | The older set was built by another transform: rebuild it with the commands named, then run the monitor again. |
| `ubo monitor`: `… has not passed the data-quality check` | A real set `ubo refresh` did not accept; a shrunken file would look like delistings. Use `--allow-unaccepted --reason` only when you have checked it. |
| `The audit log does not verify` | Nothing is shown or recorded on a broken chain. `ubo audit verify` names the first line that fails. |
| The UI cannot read the ontology | Start it with `uv run ubo ui` from `ubo-sentinel/`, or set `UBO_ONTOLOGY`. |
| A database lock error during `ubo ingest` | The UI holds no connection while idle; wait for a screen in progress to finish. |
