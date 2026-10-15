"""One form for a jurisdiction, whatever the source wrote."""

import pytest

from ubo_sentinel.pipeline.jurisdiction import normalise_jurisdiction


@pytest.mark.parametrize(
    ("written", "read"),
    [
        ("GB", "GB"),
        ("gb", "GB"),
        (" ru ", "RU"),
        ("US-DE", "US"),
        ("ca-qc", "CA"),
        ("", None),
        ("   ", None),
        (None, None),
        # Not a country code: no jurisdiction, never another one.
        ("suhh", None),
        ("x-kosovo", None),
        ("1A", None),
        ("United Kingdom", None),
    ],
)
def test_a_jurisdiction_is_read_as_its_country(written, read):
    assert normalise_jurisdiction(written) == read


def test_silver_holds_only_country_codes(con, graph_t0):
    values = con.execute(
        "SELECT DISTINCT jurisdiction FROM silver_entities WHERE snapshot_set_id = ?"
        " AND jurisdiction IS NOT NULL",
        [graph_t0],
    ).fetchall()
    assert values and all(normalise_jurisdiction(value) == value for (value,) in values)
