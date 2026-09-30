"""Time travel (rule D7) against the local compose stack."""

import uuid
from pathlib import Path

import pytest

from ran_lakehouse.lake.timetravel import demonstrate


@pytest.mark.lake
def test_as_of_read_returns_the_value_before_the_late_file(tmp_path: Path) -> None:
    result = demonstrate(f"test-{uuid.uuid4().hex[:8]}", tmp_path)
    before, now = result["as_of_publication"], result["now"]
    assert before["periods_reported"] == 3 and before["coverage"] == 0.75
    assert now["periods_reported"] == 4 and now["coverage"] == 1.0
    assert before["value"] != now["value"]
