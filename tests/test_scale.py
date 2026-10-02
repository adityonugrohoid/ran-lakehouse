"""Scale test report (rule E1): built from its run records by committed code."""

import json
from pathlib import Path

import pytest

from ran_lakehouse import scale, scale_report


def test_report_is_rendered_from_its_record() -> None:
    record = json.loads(scale.RECORD_JSON.read_text())
    assert scale.RECORD_MD.read_text() == scale_report.render_markdown(record)


def test_derived_figures_come_from_the_runs() -> None:
    record = json.loads(scale.RECORD_JSON.read_text())
    rebuilt = scale_report.record(
        record["runs"]["slice"],
        record["runs"]["full"],
        record["lake"]["slice"],
        record["lake"]["full"],
    )
    assert json.loads(json.dumps(rebuilt)) == record


def test_power_law_goes_through_both_points() -> None:
    b, y = scale_report.power_law(10.0, 100.0, 20.0, 400.0, 40.0)
    assert b == pytest.approx(2.0)
    assert y == pytest.approx(1600.0)


def test_folder_bytes_counts_a_tree_and_an_absent_folder(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.tmp").write_bytes(b"12345")
    (tmp_path / "y.tmp").write_bytes(b"123")
    assert scale.folder_bytes(tmp_path) == 8
    assert scale.folder_bytes(tmp_path / "missing") == 0


def test_a_spill_not_sampled_says_so() -> None:
    assert scale_report.spill("silver", {}) == scale_report.SPILL_NOT_RECORDED
    assert scale_report.spill("gold", {}) == "not recorded in this run"
    assert scale_report.spill("silver", {"peak_spill_mb": 2755}) == "2,755"
