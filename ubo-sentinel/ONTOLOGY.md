# Ontology

The machine-readable source is [ontology/ontology.yaml](ontology/ontology.yaml). It is validated by `load_ontology()` in [src/ubo_sentinel/models/ontology.py](src/ubo_sentinel/models/ontology.py), and a test checks that it agrees with the Pydantic models in [src/ubo_sentinel/models/](src/ubo_sentinel/models/). This page describes the same content for people.

## Nodes

| Label | Meaning |
|---|---|
| `LegalEntity` | A company, holding, fund or other legal person |
| `Person` | A natural person (an owner, controller or designated individual) |

Both labels have the same properties: `id`, `lei`, `registration_authority_id`, `registration_number`, `legal_name`, `aliases`, `jurisdiction`, `status`, `entity_type`, `repex_reason`.

- `id` is `lei:<LEI>` when the entity has an LEI, otherwise `<source>:<source_record_id>`.
- `status` is one of `ACTIVE`, `INACTIVE`, `LAPSED`, `RETIRED`, `UNKNOWN`, kept as the source published it. `LAPSED` and `RETIRED` are data gaps.
- `repex_reason` summarises why a parent is not reported: the distinct `CATEGORY:reason` values, sorted and joined with `; `. The full reporting exceptions are separate records, each with a category (`DIRECT_PARENT` or `ULTIMATE_PARENT`), a reason and its own provenance.

### One entity from several records

- Records that share an LEI are one entity. So are records that share a registration authority and number, unless that would put two LEIs or two entity types in one entity; those two entities are linked instead.
- The entity is described by one of its records: a registry's before a list's, then the newest. A field that record leaves empty comes from the next record. `aliases` holds every other name and alias of every record.
- `supporting_records` lists every record of the entity, including the one that describes it.
- Entities with similar names are never merged. A pair that scores at least 0.75 is a link with that score as its confidence, unless both have an LEI or their jurisdictions differ.

## Edges

Every edge runs subject → object.

| Edge type | Subject → object | Subject may be | Object may be |
|---|---|---|---|
| `OWNS` | owner → asset | `LegalEntity`, `Person` | `LegalEntity` |
| `CONSOLIDATED_BY` | child → parent | `LegalEntity` | `LegalEntity` |
| `CONTROLS` | controller → controlled | `LegalEntity`, `Person` | `LegalEntity` |

Properties: `id`, `subject_id`, `object_id`, `rel_type`, `pct`, `basis`, `is_ultimate`.

- `pct` is a decimal percentage, greater than 0 and at most 100, kept to 4 decimal places. It is empty when the source gives no percentage; an empty `pct` is a data gap, never zero.
- `basis` is one of `shareholding`, `accounting_consolidation`, `directorship`, `other`.
- There is one edge per subject, object, relation and `is_ultimate`. When several records report it, the newest record that gives a percentage decides it, and `supporting_records` lists them all.
- Only code under `graph/` reasons about raw edge direction. Everything else asks for "owners or controllers of X".

## Designations

A sanctions designation is not an edge. Designations are rows in the `graph_sanctions` table, keyed by node id, with properties `id`, `entity_id`, `program`, `list_date`, `list_source`, `is_active`, `match_type`, `match_confidence`.

There is one row per designation and entity it reaches. `program`, `list_date`, `list_source` and `is_active` describe the designation. `match_type` (`DIRECT`, `LEI`, `REGISTRATION`, `FUZZY_NAME`) and `match_confidence` describe how the designation was linked to that entity:

| `match_type` | The designated record | `match_confidence` |
|---|---|---|
| `DIRECT` | is the record that describes the entity | 1 |
| `LEI` | joined the entity by a shared LEI | 1 |
| `REGISTRATION` | joined the entity by registration authority and number, or shares them with an entity it could not be merged into | 1 |
| `FUZZY_NAME` | belongs to another entity, linked to this one by name | the link's confidence |

A designation crosses one link and no further.

## Provenance

Every node, edge and designation carries: `source`, `source_record_id`, `snapshot_id`, `as_of` (a date) and `confidence` (0 to 1).

## Roles

| Role | Masked fields | Read-only | Decision rights |
|---|---|---|---|
| `analyst` | `Person`: `legal_name`, `aliases`, `registration_number` | no | none |
| `reviewer` | none | no | `APPROVED`, `OVERRIDDEN`, `ESCALATED` |
| `auditor` | none | yes | none |
| `engineer` | none | no | none |

Each role also has a list of CLI commands it may run. Each build step adds the command it registers; so far `engineer` may run `ingest` and `normalize`. Only `reviewer` may hold decision rights, and the loader rejects a file that gives them to any other role.

## What the loader rejects

A missing or extra role, node label or edge type; an unknown key at any level; an edge type without both ends or without endpoint labels; a provenance field list that differs from the five above; a masked field that is not a property of its label; decision rights on a role other than `reviewer`.
