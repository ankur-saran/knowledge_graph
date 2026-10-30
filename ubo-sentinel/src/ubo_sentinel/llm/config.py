"""Which LLM features are on, the model, and where the calls go (environment only)."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# The master switch, and one flag per feature: both must be "on".
MASTER_FLAG = "UBO_LLM"
FEATURE_FLAGS = {
    "prose": "UBO_LLM_PROSE",
    "match": "UBO_LLM_MATCH",
    "intake": "UBO_LLM_INTAKE",
    "investigate": "UBO_LLM_INVESTIGATE",
}
Feature = Literal["prose", "match", "intake", "investigate"]

# Claude Haiku 5.5: short, structured, high-volume calls. `UBO_LLM_MODEL` names another.
DEFAULT_MODEL = "claude-haiku-5-5"
# The investigator (Step 14) chooses its own tool calls over many turns: Claude
# Opus 5.5. `UBO_LLM_INVESTIGATE_MODEL` names another.
DEFAULT_INVESTIGATE_MODEL = "claude-opus-5-5"
# live: call the API. record: call it and keep each response under `recordings`.
# replay: answer only from recordings, never the network (tests and `ubo eval`).
Mode = Literal["live", "record", "replay"]
DEFAULT_RECORDINGS = Path("tests/recordings/llm")


@dataclass(frozen=True)
class InvestigateBudget:
    """What one investigation may spend. Enforced in code; the model is told too."""

    model: str
    max_steps: int
    max_tokens: int
    max_seconds: float


@dataclass(frozen=True)
class LLMConfig:
    features: frozenset[str]
    model: str
    mode: Mode
    recordings: Path
    timeout_seconds: float
    max_tokens: int
    investigate: InvestigateBudget

    def enabled(self, feature: Feature) -> bool:
        return feature in self.features


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("on", "1", "true", "yes")


def _positive(name: str, default: str, kind: type[int] | type[float]) -> int | float:
    value = kind(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be above 0, not {value}.")
    return value


def load_config() -> LLMConfig:
    """Raises `ValueError` on a mode, timeout, token limit or budget that cannot be read."""
    mode = os.environ.get("UBO_LLM_MODE", "live").strip().lower()
    if mode not in ("live", "record", "replay"):
        raise ValueError(f"UBO_LLM_MODE must be live, record or replay, not '{mode}'.")
    features = (
        frozenset(name for name, flag in FEATURE_FLAGS.items() if _on(flag))
        if _on(MASTER_FLAG)
        else frozenset()
    )
    return LLMConfig(
        features=features,
        model=os.environ.get("UBO_LLM_MODEL", "").strip() or DEFAULT_MODEL,
        mode=mode,  # type: ignore[arg-type]
        recordings=Path(os.environ.get("UBO_LLM_RECORDINGS") or DEFAULT_RECORDINGS),
        timeout_seconds=float(os.environ.get("UBO_LLM_TIMEOUT", "60")),
        max_tokens=int(os.environ.get("UBO_LLM_MAX_TOKENS", "4000")),
        investigate=InvestigateBudget(
            model=os.environ.get("UBO_LLM_INVESTIGATE_MODEL", "").strip()
            or DEFAULT_INVESTIGATE_MODEL,
            # The PRD's proposed budget: 12 tool steps and 90 s per question.
            max_steps=int(_positive("UBO_LLM_INVESTIGATE_MAX_STEPS", "12", int)),
            max_tokens=int(_positive("UBO_LLM_INVESTIGATE_MAX_TOKENS", "200000", int)),
            max_seconds=float(_positive("UBO_LLM_INVESTIGATE_MAX_SECONDS", "90", float)),
        ),
    )
