"""Published formats, end to end: the sample refresh directory through every command.

GLEIF's files and an OpenSanctions file are ingested as one real set,
normalised, built into a graph and screened; each target's outcome follows
from what the two sources say about it together.
"""

import json
import shutil

import pytest
from e2e import cli, run_python
from real_format import SAMPLES, lei, restamp

from ubo_sentinel.pipeline.bronze import resolve_set
from ubo_sentinel.pipeline.db import connect

pytestmark = pytest.mark.integration

OS = "opensanctions:NK-fx"

# target -> (recommendation, reasons, why).
EXPECTED = {
    f"lei:{lei('ORV')}": ("ESCALATE", ["DIRECT_DESIGNATION"], "OFAC names it by its LEI"),
    f"lei:{lei('ORVDE')}": (
        "REVIEW",
        ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"],
        "consolidated, with no share, into a designated parent",
    ),
    f"{OS}OrvMarine": ("ESCALATE", ["DERIVED_50PCT"], "50 % owned by a designated company"),
    f"{OS}SarvRefining": ("ESCALATE", ["DERIVED_50PCT"], "25-50 % read at its upper bound"),
    f"{OS}SarvLogistics": (
        "REVIEW",
        ["CONTROL_LINK", "DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"],
        "owned by a designated company, share not stated",
    ),
    f"{OS}MiroTrading": (
        "REVIEW",
        ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"],
        "a 'majority' is not a number",
    ),
    f"lei:{lei('SARV')}": (
        "ESCALATE",
        ["DIRECT_DESIGNATION"],
        "a list record of the same name and jurisdiction",
    ),
    f"lei:{lei('MIRO')}": (
        "REVIEW",
        ["DECISION_RELEVANT_GAP", "POSSIBLY_BLOCKED"],
        "a list record of the same name and no jurisdiction",
    ),
    f"lei:{lei('HALV')}": (
        "REVIEW",
        ["OTHER_REGIME_DESIGNATION"],
        "60 % owned by a company the EU alone designates",
    ),
    f"{OS}Kestovar": ("REVIEW", ["OTHER_REGIME_DESIGNATION"], "designated by the UK alone"),
    f"{OS}Teverin": ("CLEAR", [], "its only designation ended before the publish date"),
    f"{OS}Lomanov": ("ESCALATE", ["DIRECT_DESIGNATION"], "a designated person"),
    f"lei:{lei('BRANCH')}": ("CLEAR", [], "nothing above it is designated"),
    f"lei:{lei('KOLM')}": ("CLEAR", [], "its duplicate LEI joined it"),
}


def build(path=SAMPLES):
    for command in (
        ["ingest", "--source", "real", "--path", path],
        ["normalize", "--snapshot", "latest"],
        ["build-graph", "--snapshot", "latest"],
    ):
        result = cli(*command)
        assert result.exit_code == 0, result.output


def screen(target):
    result = cli("screen", "--target-id", target, "--snapshot", "latest", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output[result.output.index("{") :])


@pytest.mark.parametrize("target", EXPECTED)
def test_each_target_screens_as_its_sources_say(target):
    build()
    recommendation, reasons, why = EXPECTED[target]
    decision = screen(target)
    assert (decision["recommendation"], decision["reasons"]) == (recommendation, reasons), why


def test_a_real_decision_replays_identically():
    build()
    decision = screen(f"lei:{lei('HALV')}")
    assert decision["snapshot_ids"].keys() == {
        "gleif_l1",
        "gleif_rr",
        "gleif_repex",
        "os_entities",
        "os_sanctions",
        "os_relationships",
    }
    for args in ([], ["--deep", "--role", "reviewer"]):
        replayed = cli("audit", "replay", decision["decision_id"], *args)
        assert f"Replay identical: decision {decision['decision_id']}" in replayed.output


def test_two_databases_build_the_same_graph(tmp_path):
    """The same files give the same set, Silver and Gold, on any machine."""
    code = """
import json, sys
sys.path.insert(0, "tests")
from e2e import cli
from ubo_sentinel.pipeline.db import connect
for c in (["ingest", "--source", "real", "--path", "tests/data/real_format"],
          ["normalize", "--snapshot", "latest"], ["build-graph", "--snapshot", "latest"]):
    assert cli(*c).exit_code == 0
con = connect(read_only=True)
print(json.dumps(con.execute(
    "SELECT b.snapshot_set_id, b.silver_digest, g.gold_digest FROM silver_builds b"
    " JOIN gold_builds g USING (snapshot_set_id)").fetchall()))
"""
    first, second = (
        run_python(code, UBO_DB=str(tmp_path / name / "ubo.duckdb"), UBO_VAR=str(tmp_path / name))
        for name in ("a", "b")
    )
    assert first == second
    assert len(json.loads(first)) == 1


def test_a_first_ingest_of_one_source_is_refused():
    result = cli("ingest", "--source", "opensanctions", "--path", SAMPLES)
    assert result.exit_code == 1
    assert "no earlier 'real' snapshot set" in result.output


def test_a_later_list_takes_the_registry_from_the_last_set(tmp_path):
    build()
    con = connect(read_only=True)
    first = resolve_set(con, "latest")
    con.close()

    refresh = shutil.copytree(SAMPLES, tmp_path / "refresh")
    ftm = refresh / "opensanctions/entities.ftm.json"
    # A new list: Teverin's UK designation is extended.
    ended, extended = b'"endDate":["2026-03-31"]', b'"endDate":["2027-03-31"]'
    ftm.write_bytes(ftm.read_bytes().replace(ended, extended))
    restamp(refresh)
    result = cli("ingest", "--source", "opensanctions", "--path", refresh)
    assert result.exit_code == 0, result.output
    assert "inherited" in result.output

    con = connect(read_only=True)
    later = resolve_set(con, "latest")
    con.close()
    assert later.base_set_id == first.snapshot_set_id
    for name in ("gleif_l1", "gleif_rr", "gleif_repex"):
        assert later.snapshot_ids[name] == first.snapshot_ids[name]
    assert later.snapshot_ids["os_sanctions"] != first.snapshot_ids["os_sanctions"]

    for command in (["normalize", "--snapshot", "latest"], ["build-graph", "--snapshot", "latest"]):
        assert cli(*command).exit_code == 0
    # Teverin's designation no longer ends before the publish date.
    assert screen(f"{OS}Teverin")["reasons"] == ["OTHER_REGIME_DESIGNATION"]


# --- ubo refresh ------------------------------------------------------------------------


def aliases(output):
    """Alias -> set id, as `ubo refresh` prints them."""
    found = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "->" and parts[0] in ("latest", "real"):
            found[parts[0]] = parts[2]
    return found


def test_refresh_downloads_builds_checks_and_makes_the_set_real(publishers):
    result = cli("refresh")
    assert result.exit_code == 0, result.output
    assert "Data quality: snapshot set" in result.output
    assert "targets_without_designation" in result.output
    real = aliases(result.output)["real"]
    con = connect(read_only=True)
    assert resolve_set(con, "real").snapshot_set_id == real
    con.close()
    decision = cli("screen", "--target-id", f"lei:{lei('ORV')}", "--snapshot", "real", "--json")
    assert json.loads(decision.output[decision.output.index("{") :])["snapshot_set_id"] == real


def test_a_refresh_whose_list_shrank_leaves_real_where_it_was(tmp_path):
    first = cli("refresh", "--path", SAMPLES)
    assert first.exit_code == 0, first.output
    real = aliases(first.output)["real"]

    refresh = shutil.copytree(SAMPLES, tmp_path / "refresh")
    ftm = refresh / "opensanctions/entities.ftm.json"
    # A truncated list: the designation records are gone but one.
    lines = ftm.read_bytes().splitlines(keepends=True)
    sanctions = [line for line in lines if b'"schema":"Sanction"' in line]
    ftm.write_bytes(b"".join(line for line in lines if line not in sanctions[1:]))
    restamp(refresh)

    refused = cli("refresh", "--path", refresh)
    assert refused.exit_code == 1
    assert "REFUSED: active_designations fell" in refused.output
    assert "'real' is unchanged" in refused.output
    assert aliases(refused.output)["real"] == real
    assert aliases(refused.output)["latest"] != real

    no_reason = cli("refresh", "--path", refresh, "--accept-shrink")
    assert no_reason.exit_code == 2 and "needs --reason" in no_reason.output
    accepted = cli("refresh", "--path", refresh, "--accept-shrink", "--reason", "list delisted")
    assert accepted.exit_code == 0, accepted.output
    assert "accepted: list delisted" in accepted.output
    assert aliases(accepted.output)["real"] == aliases(accepted.output)["latest"] != real


def test_a_refresh_of_a_bad_directory_writes_nothing(tmp_path):
    result = cli("refresh", "--path", tmp_path)
    assert result.exit_code == 1 and "Ingest failed, nothing was written" in result.output
