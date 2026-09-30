"""Retries of lake writes (lake.catalog.write) and reads (lake.catalog.read)."""

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
    monkeypatch.setattr(catalog, "READ_RETRY_WAIT_S", 0.0)


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


def test_a_read_without_credentials_stays_local() -> None:
    con = duckdb.connect()
    catalog.use_local_s3(con)
    with pytest.raises(duckdb.Error) as failure:
        con.execute("SELECT * FROM read_parquet('s3://warehouse/missing/file.parquet')")
    message = str(failure.value)
    assert "localhost:8333" in message
    assert "amazonaws" not in message


class RefusingConnection:
    def __init__(self, failures: int, message: str) -> None:
        self.failures = failures
        self.message = message
        self.runs = 0

    def execute(self, sql: str, params: dict[str, object]) -> "RefusingConnection":
        self.runs += 1
        if self.runs <= self.failures:
            raise duckdb.HTTPException(self.message)
        return self


REFUSED = (
    "HTTP Error: HTTP GET error reading 'http://localhost:8333/warehouse/x.parquet' "
    "(HTTP 403 Forbidden)\n\nInvalidAccessKeyId: The access key ID you provided does not exist"
)


def test_a_refused_vended_key_is_read_again_once() -> None:
    before = catalog.read_retries["count"]
    con = RefusingConnection(1, REFUSED)
    assert catalog.read(con, "SELECT 1", {}) is con  # type: ignore[arg-type, comparison-overlap]
    assert con.runs == 2
    assert catalog.read_retries["count"] == before + 1


def test_a_key_refused_twice_fails_loudly() -> None:
    con = RefusingConnection(2, REFUSED)
    with pytest.raises(duckdb.HTTPException, match="InvalidAccessKeyId"):
        catalog.read(con, "SELECT 1", {})  # type: ignore[arg-type]
    assert con.runs == 2


def test_other_read_failures_are_not_retried() -> None:
    con = RefusingConnection(1, "HTTP Error: HTTP GET error (HTTP 404 Not Found)")
    with pytest.raises(duckdb.HTTPException, match="404"):
        catalog.read(con, "SELECT 1", {})  # type: ignore[arg-type]
    assert con.runs == 1
