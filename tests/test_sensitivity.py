"""What-if sensitivity report (rule M6)."""

import json

import numpy as np

from ran_lakehouse.faults import sensitivity


def test_report_is_rendered_from_its_record() -> None:
    record = json.loads(sensitivity.RECORD_JSON.read_text())
    assert sensitivity.RECORD_MD.read_text() == sensitivity.render_markdown(record)


def test_the_verdict_follows_the_p90() -> None:
    assert "do not routinely" in sensitivity.routine({"relative, %": {"p90": 9.9}})
    assert "routinely move" in sensitivity.routine({"relative, %": {"p90": 10.1}})


def test_spread_ignores_missing_values() -> None:
    s = sensitivity.spread([1.0, -3.0, float("nan"), 2.0])
    assert s == {"median": 2.0, "p90": 2.8, "p99": 2.98, "max": 3.0, "count": 3}
    assert sensitivity.spread([float("nan")]) is None
    assert np.isnan(sensitivity.relative({"before": {"k": 0.0}, "after": {"k": 1.0}}, "k"))
