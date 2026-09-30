"""Commit retries of lake writes (lake.catalog.write)."""

import duckdb
import pytest

from ran_lakehouse.lake import catalog


class FlakyConnection:
    def __init__(self, failures: int, message: str) -> None:
        self.failures = failures
        self.message = message
        self.runs = 0

    def execute(self, sql: str) -> None:
        self.runs += 1
        if self.runs <= self.failures:
            raise duckdb.TransactionException(self.message)


CONFLICT = "Failed to commit Iceberg transaction: 409 CatalogCommitConflicts"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "COMMIT_RETRY_WAIT_S", 0.0)


def test_a_rejected_commit_is_run_again() -> None:
    before = catalog.commit_retries["count"]
    con = FlakyConnection(2, CONFLICT)
    catalog.write(con, "INSERT INTO t VALUES (1)")  # type: ignore[arg-type]
    assert con.runs == 3
    assert catalog.commit_retries["count"] == before + 2


def test_retries_are_bounded() -> None:
    con = FlakyConnection(catalog.COMMIT_ATTEMPTS, CONFLICT)
    with pytest.raises(duckdb.TransactionException, match="CatalogCommitConflicts"):
        catalog.write(con, "INSERT INTO t VALUES (1)")  # type: ignore[arg-type]
    assert con.runs == catalog.COMMIT_ATTEMPTS


def test_other_failures_are_not_retried() -> None:
    con = FlakyConnection(1, "Failed to commit: 500 internal error")
    with pytest.raises(duckdb.TransactionException, match="500"):
        catalog.write(con, "INSERT INTO t VALUES (1)")  # type: ignore[arg-type]
    assert con.runs == 1
