"""Registry scale: 200 000 companies and 50 000 list names, end to end, within bounds.

A generated world (`tests/scale_world.py`) in the published formats goes
through ingest, normalize and build-graph, then is screened and searched. Each
stage is held to a bound some four times what it took when this was written
(8.7 s ingest, 15.6 s Silver, 20 s Gold, 0.4 s a screen, 0.1 s a search, on an
8-core laptop): far enough above to survive a busy machine, near enough to
catch a join or a block search that turned quadratic.

The outcomes are checked too, so the bounds are not met by doing less.
"""

import time

import pytest
from scale_world import LEAF, company, lei, name, write_world

from ubo_sentinel.agents.base import ScreenRequest
from ubo_sentinel.agents.bench import percentile
from ubo_sentinel.agents.supervisor import Recommended, Supervisor
from ubo_sentinel.audit.log import AuditLog
from ubo_sentinel.cli.app import ONTOLOGY_PATH
from ubo_sentinel.graph.entity_resolution import resolve
from ubo_sentinel.graph.store import GraphStore
from ubo_sentinel.models import load_ontology
from ubo_sentinel.pipeline.db import connect
from ubo_sentinel.pipeline.gold import build_gold
from ubo_sentinel.pipeline.loaders.real_loader import ingest_all
from ubo_sentinel.pipeline.transformer import build_silver

pytestmark = [pytest.mark.integration, pytest.mark.slow]

GROUPS = 200
LIST_NAMES = 50_000

# Seconds.
INGEST_BOUND = 40.0
SILVER_BOUND = 60.0
GOLD_BOUND = 80.0
SCREEN_P95_BOUND = 2.0
SEARCH_P95_BOUND = 0.5

# (group, company) -> (recommendation, reasons, gaps it must have).
SCREENS = {
    (0, 0): ("ESCALATE", ["DIRECT_DESIGNATION"], set()),
    # Consolidated, five levels down, with no share: possibly blocked.
    (10, LEAF): ("REVIEW", ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"], {"NULL_PCT_EDGE"}),
    (5, 0): ("ESCALATE", ["DERIVED_50PCT"], set()),
    # The designated 60 % owner is a sixth step up, past max_depth 5: a gap, not a pass.
    (15, LEAF): ("REVIEW", ["DECISION_RELEVANT_GAP"], {"DEPTH_LIMIT_REACHED"}),
    (3, 0): ("REVIEW", ["OTHER_REGIME_DESIGNATION"], set()),
    (13, LEAF): ("REVIEW", ["OTHER_REGIME_DESIGNATION"], set()),
    # A namesake on the list with no country: reviewed, never blocked.
    (7, 500): (
        "REVIEW",
        ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"],
        {"REVIEW_BAND_MATCH"},
    ),
    **{(g, LEAF): ("CLEAR", [], set()) for g in (1, 2, 4, 6, 8, 17, 99, 199)},
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """The world built once, into a database of its own; returns (database, timings)."""
    work = tmp_path_factory.mktemp("scale")
    refresh = write_world(work / "refresh", groups=GROUPS, list_names=LIST_NAMES)
    database = work / "db" / "ubo.duckdb"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("UBO_DB", str(database))
        con = connect()
        try:
            timings = {}
            started = time.perf_counter()
            ingested = ingest_all(con, refresh)
            timings["ingest"] = time.perf_counter() - started
            started = time.perf_counter()
            silver = build_silver(con, ingested.snapshot_set, load_ontology(ONTOLOGY_PATH))
            timings["silver"] = time.perf_counter() - started
            started = time.perf_counter()
            gold = build_gold(con, ingested.snapshot_set)
            timings["gold"] = time.perf_counter() - started
        finally:
            con.close()
    return database, timings, ingested, silver.counts, gold.counts


@pytest.fixture
def scale_db(world, monkeypatch):
    monkeypatch.setenv("UBO_DB", str(world[0]))
    return world


def test_every_record_arrives(world):
    _, _, ingested, silver, gold = world
    rows = {d.dataset: (d.row_count, d.reject_count) for d in ingested.datasets}
    assert rows["gleif_l1"] == (GROUPS * 1_000, 0)
    assert rows["gleif_rr"] == (GROUPS * 999, 0)
    assert rows["os_entities"][0] == LIST_NAMES + 100
    # Twenty list records joined their registry company by LEI; none merged by name.
    assert silver["entities"] == GROUPS * 1_000 + LIST_NAMES + 100 - 60
    assert silver["relationships"] == GROUPS * 999 + 20
    assert silver["rejects"] == {}
    assert gold["graph_nodes"] == silver["entities"]


def test_name_blocks_stay_bounded(world):
    """Blocks of tens of thousands of names: tried rarest first, the rest counted, not searched."""
    silver = world[3]
    assert silver["link_blocks_skipped"] > 0
    # Each namesake linked; the 50 000 other list names link to nothing.
    assert 20 <= silver["links"] < 1_000


@pytest.mark.parametrize(
    ("stage", "bound"),
    [("ingest", INGEST_BOUND), ("silver", SILVER_BOUND), ("gold", GOLD_BOUND)],
)
def test_each_stage_is_within_its_bound(world, stage, bound):
    assert world[1][stage] < bound, world[1]


def test_screens_are_right_and_within_their_bound(scale_db, ontology, tmp_path):
    supervisor = Supervisor(ontology, AuditLog(tmp_path / "log"))
    con = connect(read_only=True)
    seconds, wrong = [], []
    try:
        for (group, k), (recommendation, reasons, gaps) in SCREENS.items():
            target = f"lei:{lei(company(group, k))}"
            started = time.perf_counter()
            outcome = supervisor.run(ScreenRequest(target_id=target, snapshot="latest"), con)
            seconds.append(time.perf_counter() - started)
            assert isinstance(outcome, Recommended)
            decision = outcome.decision
            found = {gap.code for gap in decision.data_gaps}
            if (decision.recommendation, decision.reasons) != (recommendation, reasons) or not (
                gaps <= found
            ):
                wrong.append((group, k, decision.recommendation, decision.reasons, found))
    finally:
        con.close()
    assert wrong == []
    assert percentile(seconds, 0.95) < SCREEN_P95_BOUND, seconds


def test_searches_find_their_company_within_their_bound(scale_db):
    seconds, wrong = [], []
    with GraphStore.open("latest") as store:
        for i in (company(1, 7), company(42, LEAF), company(150, 3), 123_456, 199_999):
            started = time.perf_counter()
            found = resolve(store, name(i))
            seconds.append(time.perf_counter() - started)
            if not found or found[0].entity_id != f"lei:{lei(i)}":
                wrong.append((name(i), found[0].entity_id if found else None))
        started = time.perf_counter()
        assert resolve(store, "Qorvantine Blusk Holdings Ltd") == []
        seconds.append(time.perf_counter() - started)
    assert wrong == []
    assert percentile(seconds, 0.95) < SEARCH_P95_BOUND, seconds
