"""Planted-case report (rule E3): built from the run records, citing tests that exist."""

import ast
import json
from pathlib import Path

from ran_lakehouse.lake import planted_report
from ran_lakehouse.lake.silver_eval import KINDS

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_report_is_rendered_from_its_record() -> None:
    record = json.loads(planted_report.RECORD_JSON.read_text())
    assert planted_report.RECORD_MD.read_text() == planted_report.render_markdown(record)


def test_record_matches_the_run_records() -> None:
    record = json.loads(planted_report.RECORD_JSON.read_text())
    assert record == json.loads(json.dumps(planted_report.build())), (
        "a run record changed: run `uv run python -m ran_lakehouse.lake.planted_report`"
    )


def test_every_rule_cites_tests_that_exist() -> None:
    assert sorted(planted_report.RULES) == [f"D{n}" for n in range(1, 9)]
    for rule, spec in planted_report.RULES.items():
        assert spec["tests"], rule
        for cited in spec["tests"]:
            path, name = cited.split("::")
            function, _, parameter = name.partition("[")
            tree = ast.parse((REPO_ROOT / path).read_text())
            names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            assert function in names, (rule, cited)
            if parameter:
                assert parameter.rstrip("]") in KINDS, (rule, cited)
