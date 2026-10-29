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
}
Feature = Literal["prose", "match", "intake"]

# Claude Haiku 4.5: short, structured, high-volume calls. `UBO_LLM_MODEL` names another.
DEFAULT_MODEL = "claude-haiku-4-5"
# live: call the API. record: call it and keep each response under `recordings`.
# replay: answer only from recordings, never the network (tests and `ubo eval`).
Mode = Literal["live", "record", "replay"]
DEFAULT_RECORDINGS = Path("tests/recordings/llm")


@dataclass(frozen=True)
class LLMConfig:
    features: frozenset[str]
    model: str
    mode: Mode
    recordings: Path
    timeout_seconds: float
    max_tokens: int

    def enabled(self, feature: Feature) -> bool:
        return feature in self.features


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("on", "1", "true", "yes")


def load_config() -> LLMConfig:
    """Raises `ValueError` on a mode, timeout or token limit that cannot be read."""
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
    )
