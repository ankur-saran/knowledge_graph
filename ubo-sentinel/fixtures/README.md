# Fixtures

Synthetic data for the offline demo and the tests. **Every name, LEI and designation here is invented.** Nothing in these files describes a real person, company or sanctions list.

- `snapshot_t0/` — `entities.csv`, `relationships.csv`, `sanctions.csv`, `repex.csv`
- `snapshot_t1/` — `sanctions.csv` only: the same list with one designation added, one removed and one changed. The other three files are taken from `snapshot_t0/`.

What each scenario must produce is in [../eval/gold_scenarios.yaml](../eval/gold_scenarios.yaml). The files are checked by `uv run pytest tests/unit/test_fixtures.py -v`.

## Columns

The files are source-shaped: they hold what a source publishes. Canonical ids, `repex_reason`, `match_type` and `match_confidence` are derived by the pipeline and are not columns. The row schemas and the reader are in [../src/ubo_sentinel/models/fixture_rows.py](../src/ubo_sentinel/models/fixture_rows.py).

Every file starts with `source`, `source_record_id`, `as_of`, `confidence`. Then:

| File | Columns |
|---|---|
| `entities.csv` | `lei`, `registration_authority_id`, `registration_number`, `legal_name`, `aliases`, `jurisdiction`, `status`, `entity_type` |
| `relationships.csv` | `subject_ref`, `object_ref`, `rel_type`, `pct`, `basis`, `is_ultimate` |
| `sanctions.csv` | `entity_ref`, `program`, `list_date`, `list_source`, `is_active` |
| `repex.csv` | `entity_ref`, `category`, `reason` |

- A `*_ref` is `<source>:<source_record_id>` of a row in `entities.csv`.
- An empty cell means "not given". An empty `pct` is unknown, never 0.
- `aliases` are separated by `|`. Dates are `YYYY-MM-DD`. Booleans are `true` / `false`.
- Edge direction follows the ontology: `OWNS` owner → asset, `CONSOLIDATED_BY` child → parent, `CONTROLS` controller → controlled.

## Conventions

- **Two sources.** `fx_registry` stands in for a registry such as GLEIF; `fx_list` stands in for a sanctions dataset such as OpenSanctions. One entity may have a record in each. Records that share an LEI, or a registration authority and number, are the same entity; records that only have similar names are not.
- **One scenario per group of rows.** `source_record_id` starts with the scenario: `s03-owner-a` belongs to `SCEN-03`. Rows of one scenario never refer to rows of another. `s00-*` is background data with no scenario.
- **Names are distinctive on purpose.** Two different entities may have similar names only where a gold scenario declares the pair. Avoid reusing words such as "Holdings" or "Trading" across scenarios.
- **LEIs** are 20 characters, start with `FX` and are not valid real LEIs.
- **Bytes matter.** Snapshot ids are computed from file bytes. Keep the files UTF-8 without a BOM, with LF line endings (`.gitattributes` enforces this on checkout).
- **Aliases.** `uv run ubo ingest --source fixtures` loads `snapshot_t0/`, and the `fixtures` alias points at that set. Loading `snapshot_t1/` (`--path fixtures/snapshot_t1`) makes a new set and moves only `latest`, so `fixtures` always means t0.
- **Row keys are stable.** `source_record_id` identifies a row across snapshots; the list diff relies on it.
