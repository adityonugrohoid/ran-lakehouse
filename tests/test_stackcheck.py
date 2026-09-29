"""The stack check report and its synthetic data, without the compose stack."""

import json

import pyarrow.compute as pc

from ran_lakehouse import stackcheck


def test_report_markdown_matches_record() -> None:
    record = json.loads(stackcheck.RESULTS_JSON.read_text())
    assert stackcheck.RESULTS_MD.read_text() == stackcheck.render_markdown(record)


def test_synthetic_batches_are_deterministic() -> None:
    first_initial, first_late = stackcheck.synthetic_batches()
    second_initial, second_late = stackcheck.synthetic_batches()
    assert first_initial.equals(second_initial)
    assert first_late.equals(second_late)


def test_late_batch_shape() -> None:
    initial, late = stackcheck.synthetic_batches()
    n_periods = stackcheck.N_DAYS * stackcheck.PERIODS_PER_DAY
    assert initial.num_rows == stackcheck.N_CELLS * n_periods - stackcheck.N_LATE_MISSING
    assert late.num_rows == stackcheck.N_LATE_MISSING + stackcheck.N_LATE_RESENT
    late_period = stackcheck.period_start(stackcheck.LATE_PERIOD_INDEX)
    assert pc.all(pc.equal(late.column("period_start"), late_period)).as_py()
