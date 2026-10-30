# Fixtures

Synthetic data for the offline demo and the tests. **Every name, LEI and designation here is invented.** Nothing in these files describes a real person, company or sanctions list.

- `snapshot_t0/` — `entities.csv`, `relationships.csv`, `sanctions.csv`, `repex.csv`
- `snapshot_t1/` — `sanctions.csv` only: the same list with one designation added, one removed and one changed. The other three files are taken from `snapshot_t0/`.
- `snapshot_t2/` — `entities.csv` and `relationships.csv`, loaded after t1 (Step 12): one stake raised (SCEN-10), one list record that states an LEI and so joins the registry's record (SCEN-14), one LEI that lapsed (SCEN-16, which keeps its outcome). The sanctions and exceptions are taken from the set before it.

What each scenario must produce is in [../eval/gold_scenarios.yaml](../eval/gold_scenarios.yaml). The files are checked by `uv run pytest tests/unit/test_fixtures.py -v`.

## Columns

The files are source-shaped: they hold what a source publishes. Canonical ids, `repex_reason`, `match_type` and `match_confidence` are derived by the pipeline and are not columns. The row schemas and the reader are in [../src/ubo_sentinel/models/fixture_rows.py](../src/ubo_sentinel/models/fixture_rows.py).

Every file starts with `source`, `source_record_id`, `as_of`, `confidence`. Then:

| File | Columns |
|---|---|
| `entities.csv` | `lei`, `registration_authority_id`, `registration_number`, `legal_name`, `aliases`, `jurisdiction`, `status`, `lei_status`, `entity_type` |
| `relationships.csv` | `subject_ref`, `object_ref`, `rel_type`, `pct`, `basis`, `is_ultimate` |
| `sanctions.csv` | `entity_ref`, `program`, `list_date`, `list_source`, `is_active` |
| `repex.csv` | `entity_ref`, `category`, `reason` |

- A `*_ref` is `<source>:<source_record_id>` of a row in `entities.csv`.
- An empty cell means "not given". An empty `pct` is unknown, never 0.
- `aliases` are separated by `|`. Dates are `YYYY-MM-DD`. Booleans are `true` / `false`.
- `status` is the entity's legal status (`ACTIVE`, `INACTIVE`, `UNKNOWN`); `lei_status` is the status of its LEI record (`ISSUED`, `LAPSED`, `RETIRED`, ...). A registry row with an LEI gives its status; a list row states an LEI and leaves `lei_status` empty.
- `jurisdiction` is an ISO 3166-1 alpha-2 country code.
- `list_source` names the list a designation is on. `FX-OFAC-SDN` stands in for the OFAC SDN list, the one the OFAC pack applies; `FX-EU-LIST` stands in for a list it does not (SCEN-30).
- Edge direction follows the ontology: `OWNS` owner → asset, `CONSOLIDATED_BY` child → parent, `CONTROLS` controller → controlled.

## Conventions

- **Two sources.** `fx_registry` stands in for a registry such as GLEIF; `fx_list` stands in for a sanctions dataset such as OpenSanctions. One entity may have a record in each. Records that share an LEI, or a registration authority and number, are the same entity; records that only have similar names are not.
- **One scenario per group of rows.** `source_record_id` starts with the scenario: `s03-owner-a` belongs to `SCEN-03`. Rows of one scenario never refer to rows of another. `s00-*` is background data with no scenario.
- **Names are distinctive on purpose.** Two different entities may have similar names only where a gold scenario declares the pair. Avoid reusing words such as "Holdings" or "Trading" across scenarios.
- **LEIs** are 20 characters, start with `FX` and are not valid real LEIs.
- **Bytes matter.** Snapshot ids are computed from file bytes. Keep the files UTF-8 without a BOM, with LF line endings (`.gitattributes` enforces this on checkout).
- **Aliases.** `uv run ubo ingest --source fixtures` loads `snapshot_t0/`, and the `fixtures` alias points at that set. Loading `snapshot_t1/` (`--path fixtures/snapshot_t1`) makes a new set and moves only `latest`, so `fixtures` always means t0. `snapshot_t2/` likewise.
- **Row keys are stable.** `source_record_id` identifies a row across snapshots; the snapshot diff (`ubo monitor`) relies on it.

## `snapshot_inv/`: the investigation cases (Step 14)

t0's `entities.csv`, `relationships.csv` and `sanctions.csv` with rows added at the end (none changed), and `addresses.csv`. Load it after t0 (`uv run ubo ingest --source fixtures --path fixtures/snapshot_inv`): it takes `repex` from t0 and makes a new set, so `fixtures` stays on t0 and every t0 scenario reads as before.

| File | Columns |
|---|---|
| `addresses.csv` | `entity_ref`, `kind` (`LEGAL`, `HEADQUARTERS`, `OTHER`), `address`, `country` |

`addresses.csv` is optional in every fixture set: it is context an investigation reads (`pipeline/context.py`), never the graph a screen reads. Rows start with `i0N`; one investigation case each:

- `i01` — a clean target at the office of a designated list company, the two written two ways.
- `i02` — a formation agent's address held by 59 entities (t0's): more than `MASS_ADDRESS_LIMIT`, counted and not listed.
- `i03` — one director of the target and of a designated company; a designated person (`s03-owner-a`) at that company's address.
- `i04` — a parent that owns the target (60 %) and a designated company (70 %): a designated sibling.
- `i05` — two parties joined through an intermediate (10 %, then 30 %): a path between them.
- `i06` — an owner whose aliases are written to steer a model (the injection case), at the same address as `i03`'s designated company.
- `i00-addr-nobody` names no entity: counted as unresolved, never placed.

The generator is not kept: the files are the record. Keep each case's names distinct from every other name in the set.
