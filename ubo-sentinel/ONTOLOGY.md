# Ontology

The machine-readable source is [ontology/ontology.yaml](ontology/ontology.yaml). It is validated by `load_ontology()` in [src/ubo_sentinel/models/ontology.py](src/ubo_sentinel/models/ontology.py), and a test checks that it agrees with the Pydantic models in [src/ubo_sentinel/models/](src/ubo_sentinel/models/). This page describes the same content for people.

## Nodes

| Label | Meaning |
|---|---|
| `LegalEntity` | A company, holding, fund or other legal person |
| `Person` | A natural person (an owner, controller or designated individual) |

Both labels have the same properties: `id`, `lei`, `registration_authority_id`, `registration_number`, `legal_name`, `aliases`, `jurisdiction`, `status`, `entity_type`, `repex_reason`.

- `id` is `lei:<LEI>` when the entity has an LEI, otherwise `<source>:<source_record_id>`.
- `status` is one of `ACTIVE`, `INACTIVE`, `LAPSED`, `RETIRED`, `UNKNOWN`. `LAPSED` and `RETIRED` are data gaps.
- `repex_reason` summarises why a parent is not reported. The full reporting exceptions are separate records, each with a category (`DIRECT_PARENT` or `ULTIMATE_PARENT`), a reason and its own provenance.

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
- Only code under `graph/` reasons about raw edge direction. Everything else asks for "owners or controllers of X".

## Designations

A sanctions designation is not an edge. Designations are rows in the `graph_sanctions` table, keyed by node id, with properties `id`, `entity_id`, `program`, `list_date`, `list_source`, `is_active`, `match_type`, `match_confidence`.

There is one row per designation and entity it reaches. `program`, `list_date`, `list_source` and `is_active` describe the designation. `match_type` (`DIRECT`, `LEI`, `REGISTRATION`, `FUZZY_NAME`) and `match_confidence` describe how the designation was linked to that entity.

## Provenance

Every node, edge and designation carries: `source`, `source_record_id`, `snapshot_id`, `as_of` (a date) and `confidence` (0 to 1).

## Roles

| Role | Masked fields | Read-only | Decision rights |
|---|---|---|---|
| `analyst` | `Person`: `legal_name`, `aliases`, `registration_number` | no | none |
| `reviewer` | none | no | `APPROVED`, `OVERRIDDEN`, `ESCALATED` |
| `auditor` | none | yes | none |
| `engineer` | none | no | none |

Each role also has a list of CLI commands it may run. The lists are empty today; each build step adds the command it registers. Only `reviewer` may hold decision rights, and the loader rejects a file that gives them to any other role.

## What the loader rejects

A missing or extra role, node label or edge type; an unknown key at any level; an edge type without both ends or without endpoint labels; a provenance field list that differs from the five above; a masked field that is not a property of its label; decision rights on a role other than `reviewer`.
