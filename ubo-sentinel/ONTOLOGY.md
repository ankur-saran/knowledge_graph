# Ontology

Stub. Filled in Step 1 of [BUILD_PLAN.md](BUILD_PLAN.md), alongside `ontology/ontology.yaml`, which is the machine-readable source.

It will describe:

- Node labels: `LegalEntity`, `Person`
- Edge types and their direction: `OWNS` (owner → asset), `CONSOLIDATED_BY` (child → parent), `CONTROLS` (controller → controlled)
- How designations are represented: a `graph_sanctions` table keyed by node id, not edges
- The provenance fields required on every node, edge and sanction
- The four roles (`analyst`, `reviewer`, `auditor`, `engineer`) and their permission matrix: commands, visible fields, decision rights

Today `ontology/ontology.yaml` holds only the four roles, each with an empty command list.
