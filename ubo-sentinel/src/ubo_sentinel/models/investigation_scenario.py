"""Investigation scenarios (Step 14.7): a question about a screen of a fixture target, and
what an investigation of it should find."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ubo_sentinel.models.investigation import FindingCode
from ubo_sentinel.models.provenance import NonEmptyStr


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedFinding(_Model):
    code: FindingCode
    # The entities it must name, by their ids in the fixtures. A person is scored by
    # the pseudonym the analyst's view gives it. A finding may name more.
    subjects: list[NonEmptyStr] = Field(min_length=1)
    # True when the first pass, with no model, must find it: the offline gate.
    first_pass: bool = False


class InvestigationScenario(_Model):
    id: NonEmptyStr
    # Screened on the investigation set (`fixtures/snapshot_inv`).
    target_id: NonEmptyStr
    question: NonEmptyStr
    expected_findings: list[ExpectedFinding] = []
    # Codes the model must not find: what a steering name asks it to say.
    must_not_claim: list[FindingCode] = []
    # The question asks for what the tools do not hold: the model adds no finding.
    out_of_scope: bool = False
    # A name in the case is written to steer a model (i06).
    injection: bool = False
    rationale: NonEmptyStr


def load_investigation_scenarios(path: Path) -> list[InvestigationScenario]:
    """Raises `OSError`, `yaml.YAMLError`, `pydantic.ValidationError`, or `ValueError` on a
    duplicate id."""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    scenarios = TypeAdapter(list[InvestigationScenario]).validate_python(data)
    ids = [scenario.id for scenario in scenarios]
    duplicates = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate scenario ids: {', '.join(duplicates)}")
    return scenarios
