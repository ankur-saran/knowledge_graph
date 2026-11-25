"""Step 18: the entity-resolution sweep and its baseline."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ubo_sentinel.cli import eval_cmd
from ubo_sentinel.cli.app import app
from ubo_sentinel.graph.entity_resolution import (
    ALL_TYPES,
    PAIR_COLUMNS,
    SWEEP_THRESHOLDS,
    LabelledPair,
    ScoredRow,
    cut,
    load_pairs,
    report_of,
    score_pairs,
    sweep_labelled,
    sweep_pairs,
)

runner = CliRunner()

ROWS = [
    # Found first above 0.90, second from 0.80 to 0.90 (a better-scored wrong name first).
    ScoredRow("a", "x", "typo", (("y", 0.9), ("x", 0.8))),
    # Found first at any cut-off up to its score.
    ScoredRow("b", "x", "exact", (("x", 1.0),)),
    # Not found at all.
    ScoredRow("c", "x", "transliteration", ()),
    # Names no entity: a candidate at 0.86 only.
    ScoredRow("d", None, "near_homonym", (("z", 0.86),)),
    ScoredRow("e", None, "negative", ()),
]


def test_thresholds_run_from_075_to_100_by_001():
    assert SWEEP_THRESHOLDS[0] == 75 and SWEEP_THRESHOLDS[-1] == 100
    assert len(SWEEP_THRESHOLDS) == 26
    # Compared as the code compares a pack's threshold: 0.85 meets 85.
    assert cut(0.85, 85) and not cut(0.8499, 85) and cut(1.0, 100)


def test_the_sweep_cuts_the_stored_candidates_at_every_threshold():
    sweep = sweep_labelled(ROWS)
    assert sorted(sweep) == list(SWEEP_THRESHOLDS)
    at_80, at_85, at_91 = sweep[80], sweep[85], sweep[91]
    # The wrong name comes first while it is kept; the right one is still in the first five.
    assert (at_80["typo"].first, at_80["typo"].in_top_5) == (0, 1)
    # Above 0.90 neither is kept.
    assert (at_91["typo"].first, at_91["typo"].in_top_5) == (0, 0)
    assert at_85[ALL_TYPES].named == 3 and at_85[ALL_TYPES].first == 1
    assert at_85[ALL_TYPES].precision_at_1 == pytest.approx(1 / 3)
    assert at_85["near_homonym"].false_candidate_rate == 1.0
    assert sweep[87]["near_homonym"].false_candidate_rate == 0.0
    # A type with no named row has no precision, not a perfect one.
    assert at_85["negative"].precision_at_1 is None


def test_the_report_at_the_floor_keeps_negatives_strict_and_counts_near_homonyms():
    report = report_of(ROWS)
    assert (report.total, report.correct) == (3, 1)
    assert report.false_candidates == []
    assert report.homonym_candidates == [("d", "z", 0.86)]


def test_pairs_are_scored_once_and_cut_at_every_threshold():
    pairs = [
        LabelledPair("Acme Trading FZE", "ACME TRADING FZE", "LegalEntity", True, "case"),
        LabelledPair("ABC Trading Ltd", "ABD Trading Ltd", "LegalEntity", False, "near_homonym"),
        LabelledPair("Dmitri Volkanov", "Дмитрий Волканов", "Person", True, "transliteration"),
    ]
    scored = score_pairs(pairs)
    sweep = sweep_pairs(scored)
    near = next(score for pair, score in scored if pair.variant_type == "near_homonym")
    linked_at = int(near * 100)
    assert sweep[linked_at]["near_homonym"].false_pos == 1
    every = sweep[100][ALL_TYPES]
    # Identical after folding: linked at 1.00; the other script is not (yet).
    assert (every.true_pos, every.false_neg, every.false_pos) == (1, 1, 0)
    assert every.precision == 1.0 and every.recall == 0.5
    assert every.f1 == pytest.approx(2 / 3)
    # Only pairs labelled different: no recall, so no F1.
    assert sweep[100]["near_homonym"].recall is None
    assert sweep[100]["near_homonym"].f1 is None


def test_a_pair_file_must_state_its_columns_and_a_label(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text("name_a,name_b\nA,B\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lacks columns"):
        load_pairs(path)
    header = ",".join(PAIR_COLUMNS)
    path.write_text(f"{header}\nA,B,LegalEntity,yes,exact,,,,,me,s\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not 0 or 1"):
        load_pairs(path)


def test_the_shipped_pair_files_read():
    for path in eval_cmd.ER_PAIRS_PATHS.values():
        if path.is_file():
            assert load_pairs(path)


def test_the_shipped_real_labels_say_who_labelled_them_and_on_which_set():
    text = Path("eval/er_labelled_real.csv").read_text(encoding="utf-8")
    header, *rows = text.splitlines()
    assert header.endswith(",labelled_by,set_id")
    for row in rows:
        assert not row.endswith(",,"), row


def build_with_the_cli():
    for command in (["ingest", "--source", "fixtures"], ["normalize"], ["build-graph"]):
        assert runner.invoke(app, command).exit_code == 0


def test_sweep_prints_every_threshold_and_marks_the_operating_points(tmp_path, monkeypatch):
    build_with_the_cli()
    baseline = tmp_path / "er_baseline.json"
    monkeypatch.setattr(eval_cmd, "ER_BASELINE_PATH", baseline)

    result = runner.invoke(app, ["eval", "--suite", "er", "--sweep"])
    assert result.exit_code == 0, result.output
    for title in ("Precision@1", "Recall@5", "Precision", "Recall", "F1"):
        assert f"  {title}\n" in result.output
    assert "    0.75  " in result.output and "    1.00  " in result.output
    assert "    0.85 *" in result.output and "    0.92 *" in result.output
    assert "    0.86 *" not in result.output
    assert "transliteration" in result.output and not baseline.exists()

    result = runner.invoke(app, ["eval", "--suite", "er", "--sweep", "--update-baseline"])
    assert result.exit_code == 0, result.output
    written = json.loads(baseline.read_text(encoding="utf-8"))
    fixtures = written["fixtures"]
    assert set(fixtures["resolution"]) == {"0.85", "0.92"}
    assert set(fixtures["pair_scores"]) == {"0.85", "0.92"}
    assert fixtures["operating_points"] == {"match_threshold": "0.92", "review_band_low": "0.85"}
    assert fixtures["labels"]["path"] == "eval/er_labelled.csv"
    # Written again from the same inputs: the same bytes.
    before = baseline.read_bytes()
    runner.invoke(app, ["eval", "--suite", "er", "--sweep", "--update-baseline"])
    assert baseline.read_bytes() == before

    as_json = runner.invoke(app, ["eval", "--suite", "er", "--sweep", "--json"])
    document = json.loads(as_json.output)
    assert len(document["sweep"]["resolution"]) == 26
    assert document["sweep"]["operating_points"] == ["0.85", "0.92"]


def test_sweep_and_baseline_need_the_er_suite():
    for args in (["--suite", "rules", "--sweep"], ["--suite", "er", "--update-baseline"]):
        result = runner.invoke(app, ["eval", *args])
        assert result.exit_code == 2, args
