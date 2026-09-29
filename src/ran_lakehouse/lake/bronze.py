"""Bronze (rule L1): raw parsed values, immutable, with lineage.

Tables in namespace "bronze" (Iceberg through Lakekeeper, written by DuckDB):

- pm_values: one row per value in a PM file, as the file wrote it (vendor
  counter name, raw value, NULL for NIL), with the period in UTC, the
  suspect flag, the dictionary release the file declares (Huawei-style
  swVersion; NULL where the format declares none) and lineage (source
  file, hash, arrival, parser version, load id);
- file_arrivals: every delivery the collector saw, loaded or not;
- cm_records and fm_records: the CM and FM export lines as delivered.

The evaluation-only answers live in namespace "evaluation" (rule A3).

Bronze is append-only: loads only INSERT; nothing is updated or deleted.
"""

from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

BRONZE = "lk.bronze"
EVALUATION = "lk.evaluation"

TABLES = {
    f"{BRONZE}.pm_values": """(
        period_start TIMESTAMPTZ, period_end TIMESTAMPTZ, ems VARCHAR,
        managed_element VARCHAR, object_dn VARCHAR, meas_group VARCHAR,
        counter VARCHAR, value DOUBLE, suspect BOOLEAN, dictionary_release VARCHAR,
        file_name VARCHAR, file_hash VARCHAR, arrival_time TIMESTAMPTZ,
        parser_version VARCHAR, load_id VARCHAR)""",
    f"{BRONZE}.file_arrivals": """(
        arrival_time TIMESTAMPTZ, ems VARCHAR, kind VARCHAR, file_name VARCHAR,
        file_hash VARCHAR, size_bytes BIGINT, loaded BOOLEAN, rows BIGINT,
        parser_version VARCHAR, load_id VARCHAR)""",
    f"{BRONZE}.cm_records": """(
        arrival_time TIMESTAMPTZ, ems VARCHAR, kind VARCHAR, file_name VARCHAR,
        file_hash VARCHAR, record VARCHAR, load_id VARCHAR)""",
    f"{BRONZE}.fm_records": """(
        arrival_time TIMESTAMPTZ, ems VARCHAR, file_name VARCHAR, file_hash VARCHAR,
        record VARCHAR, load_id VARCHAR)""",
    f"{EVALUATION}.delivery_anomalies": """(
        kind VARCHAR, ems VARCHAR, period_start TIMESTAMPTZ, managed_element VARCHAR,
        detail VARCHAR)""",
}
PARTITIONS = {f"{BRONZE}.pm_values": "day(period_start)"}


def create_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the bronze and evaluation tables if they do not exist.

    Args:
        con: DuckDB with the warehouse attached.
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {BRONZE}")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {EVALUATION}")
    existing = {
        f"lk.{schema}.{name}"
        for schema, name in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = 'lk'"
        ).fetchall()
    }
    for table, columns in TABLES.items():
        if table in existing:
            continue
        con.execute(f"CREATE TABLE {table} {columns}")
        if table in PARTITIONS:
            con.execute(f"ALTER TABLE {table} SET PARTITIONED BY ({PARTITIONS[table]})")


def require_empty(con: duckdb.DuckDBPyConnection) -> None:
    """Fail unless bronze holds no PM values (a backfill starts from empty).

    Args:
        con: DuckDB with the warehouse attached.

    Raises:
        RuntimeError: If pm_values already has rows.
    """
    row = con.execute(f"SELECT count(*) FROM {BRONZE}.pm_values").fetchone()
    if row is None or row[0] != 0:
        raise RuntimeError(
            "bronze.pm_values is not empty; bronze is append-only, so backfill into a new "
            "warehouse (--warehouse)"
        )


def require_started(con: duckdb.DuckDBPyConnection, planned: int) -> None:
    """Fail unless the warehouse holds the run being continued.

    Args:
        con: DuckDB with the warehouse attached.
        planned: Anomalies in this run's delivery plan.

    Raises:
        RuntimeError: If no run is recorded, or one with another plan (a
            different run length).
    """
    row = con.execute(f"SELECT count(*) FROM {EVALUATION}.delivery_anomalies").fetchone()
    recorded = 0 if row is None else int(row[0])
    if recorded == 0:
        raise RuntimeError("no run in this warehouse to continue; start from day 0")
    if recorded != planned:
        raise RuntimeError(
            f"the warehouse's run planted {recorded} delivery anomalies, this run plans "
            f"{planned}: continue with the same --weeks and profile"
        )


def append(con: duckdb.DuckDBPyConnection, table: str, data: pa.Table) -> None:
    """Append rows to a table (bronze never updates in place).

    Args:
        con: DuckDB with the warehouse attached.
        table: Fully qualified table.
        data: Rows, columns in the table's order; dictionary-encoded strings
            are cast to VARCHAR.
    """
    if data.num_rows == 0:
        return
    con.register("incoming", data)
    columns = ", ".join(
        f'CAST("{f.name}" AS VARCHAR) AS "{f.name}"'
        if pa.types.is_dictionary(f.type)
        else f'"{f.name}"'
        for f in data.schema
    )
    try:
        con.execute(f"INSERT INTO {table} SELECT {columns} FROM incoming")
    finally:
        con.unregister("incoming")


def constant(value: Any, n: int, kind: pa.DataType) -> pa.Array:
    """A column holding one value n times, dictionary-encoded for strings.

    Args:
        value: The value.
        n: Length.
        kind: Arrow type of the value.

    Returns:
        The column.
    """
    if pa.types.is_string(kind):
        return pa.DictionaryArray.from_arrays(
            pa.array(np.zeros(n, dtype=np.int32)), pa.array([value], kind)
        )
    return pa.repeat(pa.scalar(value, kind), n)
