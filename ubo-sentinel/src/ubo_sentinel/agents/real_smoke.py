"""The real-data smoke suite (`eval/real_smoke.yaml`): invariants that hold on any refresh.

Real results move with every publish: a company is delisted, an owner sells.
A fixed expected recommendation would fail for reasons that have nothing to do
with the code. So an entry states what must stay true on any refresh (an
SDN-listed LEI is never CLEAR; a well-known unsanctioned issuer is never a
direct designation; a name resolves to its LEI) and keeps the recommendation
it had on the set it was verified on.

On that set a different recommendation is a failure: the code changed what it
decides. On a newer set it is drift, reported and not failed: the world
changed, or the code did, and a person should look.
"""

from pathlib import Path

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ubo_sentinel.agents.base import ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.supervisor import Recommended, Supervisor
from ubo_sentinel.graph.entity_resolution import resolve
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models.decision import Recommendation
from ubo_sentinel.models.evidence import ReasonCode
from ubo_sentinel.rules.pack import RulePack

# Who the suite screens as.
SMOKE_ROLE = "analyst"
SMOKE_ACTOR = "eval:real-smoke"


class Expect(BaseModel):
    """What must hold of the screen on any refresh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendation_in: list[Recommendation] | None = None
    recommendation_not: list[Recommendation] = []
    reasons_include: list[ReasonCode] = []
    reasons_exclude: list[ReasonCode] = []


class SmokeEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^RS-\d{2,}$")
    rationale: str = Field(min_length=1)
    # Screened as `lei:<LEI>`.
    lei: str | None = Field(default=None, pattern=r"^[A-Z0-9]{18}[0-9]{2}$")
    expect: Expect = Expect()
    # A name, resolved (with the jurisdiction, when given): the first candidate must be this id.
    query: str | None = None
    jurisdiction: str | None = None
    resolves_to: str | None = None
    # The set the entry was checked on, and what the screen recommended there.
    verified_on_set: str | None = None
    verified_recommendation: Recommendation | None = None

    @model_validator(mode="after")
    def _complete(self) -> "SmokeEntry":
        if self.lei is None and self.query is None:
            raise ValueError(f"{self.id}: an entry screens an LEI or resolves a query")
        if (self.query is None) != (self.resolves_to is None):
            raise ValueError(f"{self.id}: a query needs resolves_to, and resolves_to a query")
        if (self.verified_on_set is None) != (self.verified_recommendation is None):
            raise ValueError(f"{self.id}: verified_on_set and verified_recommendation go together")
        if self.verified_recommendation is not None and self.lei is None:
            raise ValueError(f"{self.id}: verified_recommendation needs an lei")
        return self


class SmokeFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[SmokeEntry]

    @model_validator(mode="after")
    def _unique(self) -> "SmokeFile":
        ids = [e.id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("entry ids repeat")
        return self


class SmokeReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_id: str
    recommendation: str | None = None
    reasons: list[str] = []
    resolved_to: str | None = None
    # Invariants that did not hold; empty when the entry passed.
    failures: list[str] = []
    # A different recommendation than on the verified set, on another set.
    drift: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures


def load_smoke(path: Path) -> list[SmokeEntry]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return SmokeFile.model_validate(data).entries
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc


def _check_screen(entry: SmokeEntry, recommendation: str, reasons: list[str], set_id: str):
    expect, failures = entry.expect, []
    if expect.recommendation_in is not None and recommendation not in expect.recommendation_in:
        failures.append(f"{recommendation} is not one of {expect.recommendation_in}")
    if recommendation in expect.recommendation_not:
        failures.append(f"{recommendation} must not be")
    for reason in expect.reasons_include:
        if reason not in reasons:
            failures.append(f"reason {reason} missing")
    for reason in expect.reasons_exclude:
        if reason in reasons:
            failures.append(f"reason {reason} present")
    drift = None
    verified = entry.verified_recommendation
    if verified is not None and recommendation != verified:
        if set_id == entry.verified_on_set:
            failures.append(f"{recommendation} on its verified set, where it was {verified}")
        else:
            drift = f"{verified} on {entry.verified_on_set}, {recommendation} on {set_id}"
    return failures, drift


def run_smoke(
    supervisor: Supervisor,
    entries: list[SmokeEntry],
    store: GraphStore,
    con: duckdb.DuckDBPyConnection | None = None,
    pack: RulePack | None = None,
) -> list[SmokeReport]:
    reports = []
    for entry in entries:
        failures: list[str] = []
        drift = recommendation = resolved_to = None
        reasons: list[str] = []
        if entry.lei is not None:
            request = ScreenRequest(
                target_id=f"lei:{entry.lei}",
                snapshot=store.set_id,
                role=SMOKE_ROLE,
                actor=SMOKE_ACTOR,
            )
            try:
                outcome = supervisor.run(request, con or store.con, pack)
            except ScreenUsageError as exc:
                outcome, failures = None, [f"not screened: {exc}"]
            if isinstance(outcome, Recommended):
                recommendation = outcome.decision.recommendation
                reasons = list(outcome.decision.reasons)
                found, drift = _check_screen(entry, recommendation, reasons, store.set_id)
                failures += found
            elif outcome is not None:
                failures.append(f"not screened: {type(outcome).__name__}")
        if entry.query is not None:
            found = resolve(store, entry.query, jurisdiction=entry.jurisdiction)
            resolved_to = found[0].entity_id if found else None
            if resolved_to != entry.resolves_to:
                failures.append(
                    f"'{entry.query}' resolved to {resolved_to}, not {entry.resolves_to}"
                )
        reports.append(
            SmokeReport(
                entry_id=entry.id,
                recommendation=recommendation,
                reasons=reasons,
                resolved_to=resolved_to,
                failures=failures,
                drift=drift,
            )
        )
    return reports
