"""Schema for `ontology/ontology.yaml` and its loader."""

from pathlib import Path
from typing import Literal, Self, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ubo_sentinel.models.entity import EntityType
from ubo_sentinel.models.provenance import NonEmptyStr, Provenance
from ubo_sentinel.models.relationship import RelType

Role = Literal["analyst", "reviewer", "auditor", "engineer"]
DecisionRight = Literal["APPROVED", "OVERRIDDEN", "ESCALATED"]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeSpec(_Spec):
    properties: list[NonEmptyStr] = Field(min_length=1)


class EndpointSpec(_Spec):
    # What this end of the edge is, e.g. "owner" or "asset".
    role: NonEmptyStr
    labels: list[EntityType] = Field(min_length=1)


class EdgeSpec(_Spec):
    """An edge runs subject -> object."""

    subject: EndpointSpec
    object: EndpointSpec
    properties: list[NonEmptyStr] = Field(min_length=1)


class DesignationSpec(_Spec):
    """Designations are rows in a table keyed by node id, not edges."""

    table: NonEmptyStr
    keyed_by: NonEmptyStr
    properties: list[NonEmptyStr] = Field(min_length=1)


class ProvenanceSpec(_Spec):
    fields: list[NonEmptyStr]


class RoleSpec(_Spec):
    commands: list[NonEmptyStr]
    masked_fields: dict[EntityType, list[NonEmptyStr]] = {}
    read_only: bool = False
    decision_rights: list[DecisionRight] = []


def _require_all(section: str, mapping: dict, literal: object) -> None:
    missing = set(get_args(literal)) - set(mapping)
    if missing:
        raise ValueError(f"{section} is missing: {', '.join(sorted(missing))}")


class OntologyConfig(_Spec):
    node_labels: dict[EntityType, NodeSpec]
    edge_types: dict[RelType, EdgeSpec]
    designations: DesignationSpec
    provenance: ProvenanceSpec
    roles: dict[Role, RoleSpec]

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require_all("node_labels", self.node_labels, EntityType)
        _require_all("edge_types", self.edge_types, RelType)
        _require_all("roles", self.roles, Role)

        if set(self.provenance.fields) != set(Provenance.model_fields):
            raise ValueError(
                f"provenance.fields must be exactly: {', '.join(Provenance.model_fields)}"
            )

        for role, spec in self.roles.items():
            if spec.decision_rights and role != "reviewer":
                raise ValueError(f"role '{role}' may not hold decision rights")
            for label, fields in spec.masked_fields.items():
                unknown = set(fields) - set(self.node_labels[label].properties)
                if unknown:
                    raise ValueError(
                        f"role '{role}' masks fields that {label} does not have: "
                        f"{', '.join(sorted(unknown))}"
                    )
        return self


def require_write(ontology: OntologyConfig, role: str) -> None:
    """Raise unless the role may write (a decision, a review, an audit event)."""
    if role not in ontology.roles:
        raise PermissionError(f"Unknown role '{role}'.")
    if ontology.roles[role].read_only:
        raise PermissionError(f"Role '{role}' is read-only.")


def load_ontology(path: Path) -> OntologyConfig:
    """Load and validate the ontology.

    Raises `OSError`, `yaml.YAMLError` or `pydantic.ValidationError`.
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return OntologyConfig.model_validate(data)
