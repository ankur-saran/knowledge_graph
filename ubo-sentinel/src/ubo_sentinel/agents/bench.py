"""`ubo bench`: how long screens take on a set, and where the time goes.

Each LEI of a list is screened by `target_id = lei:<LEI>`, so no query needs
resolving and none can stop for a pick. The screens share one read-only
connection, as a process serving screens would: the first is the cold one
(it pays for the first reads of the database), the rest are warm. Every round
after the first screens the same targets again.

The time of each screen is measured around the supervisor, and the trace the
supervisor keeps splits it by agent and counts the rows the graph queries
read. Nothing here changes a decision; the caller chooses the audit log, and
a scratch log keeps the real one free of benchmark screens.
"""

import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import yaml

from ubo_sentinel.agents.base import ScreenRequest, ScreenUsageError
from ubo_sentinel.agents.supervisor import NeedsDisambiguation, Recommended, Supervisor
from ubo_sentinel.rules.pack import DEFAULT_PACK, RulePack

# The PRD's budget for a screen on a registry-sized set (section 7, P3).
DEFAULT_BUDGET_SECONDS = 15.0

# Who the benchmark screens as: a role that may screen, under a name that says why.
BENCH_ROLE = "analyst"
BENCH_ACTOR = "bench"


class BenchInputError(ValueError):
    """The LEI file cannot be read."""


@dataclass(frozen=True)
class Screen:
    lei: str
    round: int
    seconds: float
    # The recommendation, or NOT_IN_GRAPH, NEEDS_PICK, FAILED.
    outcome: str
    # Agent -> seconds.
    agents: dict[str, float] = field(default_factory=dict)
    # Rows the graph queries of the screen returned.
    rows: int = 0
    paths: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("CLEAR", "REVIEW", "ESCALATE")


@dataclass(frozen=True)
class BenchReport:
    snapshot_set_id: str
    screens: list[Screen]
    budget: float
    peak_rss_bytes: int | None

    @property
    def errors(self) -> list[Screen]:
        return [s for s in self.screens if not s.ok]

    @property
    def p95(self) -> float:
        return percentile([s.seconds for s in self.screens], 0.95)

    @property
    def within_budget(self) -> bool:
        return self.p95 <= self.budget

    def summary(self) -> dict[str, Any]:
        seconds = [s.seconds for s in self.screens]
        warm = [s.seconds for s in self.screens[1:]]
        agents: dict[str, list[float]] = {}
        for s in self.screens:
            for name, value in s.agents.items():
                agents.setdefault(name, []).append(value)
        rows = [s.rows for s in self.screens]
        return {
            "snapshot_set_id": self.snapshot_set_id,
            "screens": len(self.screens),
            "errors": len(self.errors),
            "seconds": _stats(seconds),
            "cold_seconds": round(seconds[0], 4) if seconds else None,
            "warm_seconds": _stats(warm),
            "agents": {name: _stats(values) for name, values in agents.items()},
            "rows_read": {
                "mean": round(sum(rows) / len(rows), 1) if rows else 0,
                "max": max(rows, default=0),
            },
            "peak_rss_mb": None
            if self.peak_rss_bytes is None
            else round(self.peak_rss_bytes / 2**20, 1),
            "budget_seconds": self.budget,
            "within_budget": self.within_budget,
        }


def percentile(values: list[float], share: float) -> float:
    """Nearest rank: the value at or below which `share` of the values lie."""
    ordered = sorted(values)
    return ordered[math.ceil(share * len(ordered)) - 1] if ordered else 0.0


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 0.50), 4),
        "p95": round(percentile(values, 0.95), 4),
        "max": round(max(values, default=0.0), 4),
    }


def peak_rss_bytes() -> int | None:
    """The most memory this process has held, or None where it cannot be read."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        info = ctypes.windll.psapi.GetProcessMemoryInfo
        info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        info.restype = wintypes.BOOL
        if not info(current(), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Kilobytes on Linux, bytes on macOS.
    return peak if sys.platform == "darwin" else peak * 1024


def read_leis(path: Path) -> list[str]:
    """LEIs from a text file (one a line, `#` comments) or a YAML file of entries with `lei`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchInputError(f"Cannot read {path}: {exc}") from exc
    if path.suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise BenchInputError(f"{path}: not YAML ({exc}).") from exc
        entries = data.get("entries", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise BenchInputError(f"{path}: expected a list of entries.")
        leis = [e["lei"] for e in entries if isinstance(e, dict) and e.get("lei")]
    else:
        leis = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    leis = [lei.upper() for lei in leis if lei]
    if not leis:
        raise BenchInputError(f"{path}: holds no LEI.")
    return list(dict.fromkeys(leis))


def _screen(
    supervisor: Supervisor,
    lei: str,
    round_: int,
    snapshot: str,
    con: duckdb.DuckDBPyConnection,
    pack: RulePack | None,
    rule_pack: str,
) -> Screen:
    request = ScreenRequest(
        target_id=f"lei:{lei}",
        snapshot=snapshot,
        rule_pack=rule_pack,
        role=BENCH_ROLE,
        actor=BENCH_ACTOR,
    )
    started = time.perf_counter()
    try:
        outcome = supervisor.run(request, con, pack)
    except ScreenUsageError as exc:
        seconds = time.perf_counter() - started
        return Screen(lei, round_, seconds, "NOT_IN_GRAPH", detail=str(exc))
    seconds = time.perf_counter() - started
    if isinstance(outcome, Recommended):
        trace = outcome.trace
        return Screen(
            lei,
            round_,
            seconds,
            outcome.decision.recommendation,
            agents={step["agent"]: step["seconds"] for step in trace},
            rows=sum(q["rows"] for step in trace for q in step["queries"]),
            paths=len(outcome.decision.paths),
        )
    if isinstance(outcome, NeedsDisambiguation):
        return Screen(lei, round_, seconds, "NEEDS_PICK")
    return Screen(lei, round_, seconds, "FAILED", detail=f"{outcome.agent}: {outcome.error_type}")


def run_bench(
    supervisor: Supervisor,
    leis: list[str],
    snapshot: str,
    con: duckdb.DuckDBPyConnection,
    snapshot_set_id: str,
    rounds: int = 1,
    budget: float = DEFAULT_BUDGET_SECONDS,
    pack: RulePack | None = None,
    rule_pack: str = DEFAULT_PACK,
) -> BenchReport:
    """Screen every LEI, `rounds` times, on one connection."""
    screens = [
        _screen(supervisor, lei, round_, snapshot, con, pack, rule_pack)
        for round_ in range(1, rounds + 1)
        for lei in leis
    ]
    return BenchReport(snapshot_set_id, screens, budget, peak_rss_bytes())
