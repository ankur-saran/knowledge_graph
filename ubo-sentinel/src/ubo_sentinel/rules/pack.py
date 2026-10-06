"""Schema for a rule pack (`rules/*.yaml`) and its loader.

Every threshold the rule engine reads is here; none is written in Python.
"""

import re
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ubo_sentinel.models.canonical import sha256_hex
from ubo_sentinel.models.provenance import Confidence, NonEmptyStr
from ubo_sentinel.pipeline.entity_linking import LINK_FLOOR

# Commands are run from the repository root.
RULES_DIR = Path("rules")
DEFAULT_PACK = "ofac"

# Raise when a change to `rules/engine.py` alters what a pack concludes. It is
# part of `rule_pack_hash`, and so of every `decision_id`.
RULES_VERSION = 1

ThresholdPct = Annotated[Decimal, Field(gt=0, le=100)]

_PACK_NAME = re.compile(r"^[a-z0-9_]+$")


class UnknownRulePack(LookupError):
    pass


class RulePack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmptyStr
    ownership_threshold_pct: ThresholdPct
    threshold_operator: Literal[">=", ">"]
    # False: no owner's stake is added to another's; the largest one is tested.
    aggregate_blocked_owners: bool
    match_threshold: Confidence
    review_band_low: Confidence
    presume_majority_for_consolidation: bool
    gap_scope: Literal["on_path", "any"]
    review_exposure_pct: ThresholdPct
    near_miss_pct: ThresholdPct
    control_link_review: bool
    control_link_scope: Literal["upstream", "target"]
    duplicate_link_review: bool
    max_depth: int = Field(ge=1)
    max_depth_down: int = Field(ge=0)
    max_paths: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.review_band_low < LINK_FLOOR:
            # Silver stores no link below the floor, so a lower band would be silently empty.
            raise ValueError(f"review_band_low may not be below the link floor ({LINK_FLOOR})")
        if self.review_band_low > self.match_threshold:
            raise ValueError("review_band_low may not be above match_threshold")
        if self.near_miss_pct >= self.ownership_threshold_pct:
            raise ValueError("near_miss_pct must be below ownership_threshold_pct")
        return self

    @property
    def rule_pack_hash(self) -> str:
        """Over the parsed values, so a comment or a reformatted file changes nothing."""
        return sha256_hex([RULES_VERSION, self.model_dump()])


def pack_path(name: str) -> Path:
    """`ofac` -> `rules/ofac.yaml`. Raises `UnknownRulePack`."""
    path = RULES_DIR / f"{name}.yaml"
    if not _PACK_NAME.match(name) or not path.is_file():
        known = ", ".join(sorted(p.stem for p in RULES_DIR.glob("*.yaml"))) or "none"
        raise UnknownRulePack(f"No rule pack '{name}' (known packs: {known}).")
    return path


def load_rule_pack(path: Path) -> RulePack:
    """Load and validate a rule pack.

    Raises `OSError`, `yaml.YAMLError` or `pydantic.ValidationError`.
    """
    with path.open(encoding="utf-8") as f:
        return RulePack.model_validate(yaml.safe_load(f))
