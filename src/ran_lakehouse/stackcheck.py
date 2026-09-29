"""Stack check (rule S2): DuckDB, dbt and SQLMesh on Iceberg through Lakekeeper.

Runs against the local compose stack (rule S3) and measures, on synthetic
counters: an Iceberg table write, MERGE INTO for a late batch (rule D1),
time travel (rule D7), and the KPI formula change with reprocessing (rule
D6). PyIceberg reads the same tables (rule S1). The result is one record,
written as results/stack_spike.json and rendered to results/stack_spike.md.

Run: `docker compose up -d --wait`, then
`uv run --group stackcheck python -m ran_lakehouse.stackcheck`.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
from pyiceberg.catalog import load_catalog

CATALOG_URL = "http://127.0.0.1:18181"
S3_ENDPOINT = "http://localhost:8333"
# Placeholders from compose/seaweedfs-iam.json; local stack only.
S3_ACCESS_KEY = "local-dev-key"
S3_SECRET_KEY = "local-dev-secret-not-a-real-credential"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000000"

BASE_SEED = 20260930
PURPOSE_COUNTERS = 1  # rule W1 purpose id for the synthetic counters

N_CELLS = 100
N_DAYS = 14
PERIODS_PER_DAY = 96  # 15-minute granularity, granPeriod PT900S (TS 32.435)
FIRST_PERIOD = datetime(2026, 1, 5, 0, 0)
LATE_PERIOD_INDEX = 7 * PERIODS_PER_DAY + 40  # the period whose file arrives late
N_LATE_MISSING = 20  # cells whose file for that period is missing at first load
N_LATE_RESENT = 20  # cells whose file for that period is re-sent with corrections
# ASSUMPTION: attempt and success levels for the synthetic counters; they
# only need to be plausible, the check measures the stack, not the network.
MEAN_ATTEMPTS = 200.0
RRC_SUCCESS_P = 0.99
ERAB_SUCCESS_P = 0.98

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_JSON = REPO_ROOT / "results" / "stack_spike.json"
RESULTS_MD = REPO_ROOT / "results" / "stack_spike.md"
TOOLS_DIR = REPO_ROOT / "stackcheck"

# KPI formulas for the rule D6 case. Version 1 is RRC setup success only;
# version 2 is the TS 32.450 E-RAB accessibility shape (RRC success times
# E-RAB setup success), each a ratio of sums (rule L3).
FORMULAS = {
    1: "sum(rrc_succ) / sum(rrc_att)",
    2: "(sum(rrc_succ) / sum(rrc_att)) * (sum(erab_succ) / sum(erab_att))",
}


@dataclass
class Check:
    """One measured check.

    Attributes:
        engine: Tool that ran the check.
        case: What was checked.
        ok: Whether every assertion held.
        seconds: Wall-clock time of the measured operation.
        detail: What was observed.
        error: Exact error text when the check failed, else empty.
    """

    engine: str
    case: str
    ok: bool
    seconds: float
    detail: str
    error: str


@dataclass
class Record:
    """The single record the report is written from.

    Attributes:
        run_date: UTC date of the run.
        versions: Package, extension and image versions used.
        data: Size of the synthetic dataset.
        checks: Every measured check, in run order.
        notes: Observations recorded by the run.
        recommendation: Conclusions drawn from the checks.
        evidence: Sources for stack facts the checks rely on.
    """

    run_date: str
    versions: dict[str, str]
    data: dict[str, int]
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recommendation: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


class CheckFailed(AssertionError):
    """An assertion inside a check did not hold."""


def expect(condition: bool, message: str) -> None:
    """Raise CheckFailed with the message when the condition is false.

    Args:
        condition: The property that must hold.
        message: What was expected, used as the failure text.

    Raises:
        CheckFailed: If the condition is false.
    """
    if not condition:
        raise CheckFailed(message)


def http_json(method: str, path: str, body: dict[str, Any] | None) -> Any:
    """Call the Lakekeeper management API.

    Args:
        method: HTTP method.
        path: Path under the catalog URL.
        body: JSON body, or None for no body.

    Returns:
        The decoded JSON response, or None for an empty response.

    Raises:
        RuntimeError: If the server answers with an error status.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        CATALOG_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc
    return json.loads(raw) if raw else None


def ensure_bootstrapped() -> None:
    """Bootstrap Lakekeeper once; later calls see it already bootstrapped.

    Raises:
        RuntimeError: If the server is not reachable or bootstrap fails.
    """
    info = http_json("GET", "/management/v1/info", None)
    if not info["bootstrapped"]:
        http_json("POST", "/management/v1/bootstrap", {"accept-terms-of-use": True})


def create_warehouse(name: str) -> None:
    """Create a warehouse on the SeaweedFS bucket with STS credential vending.

    Args:
        name: Warehouse name, also used as the key prefix.
    """
    http_json(
        "POST",
        "/management/v1/warehouse",
        {
            "warehouse-name": name,
            "project-id": DEFAULT_PROJECT_ID,
            "storage-profile": {
                "type": "s3",
                "bucket": "warehouse",
                "key-prefix": name,
                "endpoint": S3_ENDPOINT,
                "sts-endpoint": S3_ENDPOINT,
                "sts-role-arn": "arn:aws:iam::000000000000:role/LakekeeperVendedRole",
                "region": "local-01",
                "path-style-access": True,
                "flavor": "s3-compat",
                "sts-enabled": True,
            },
            "storage-credential": {
                "type": "s3",
                "credential-type": "access-key",
                "access-key-id": S3_ACCESS_KEY,
                "secret-access-key": S3_SECRET_KEY,
            },
            "delete-profile": {"type": "hard"},
        },
    )


def period_start(index: int) -> datetime:
    """Return the start of a 15-minute period.

    Args:
        index: Period index from FIRST_PERIOD.

    Returns:
        The period start time.
    """
    return FIRST_PERIOD + timedelta(minutes=15 * index)


def cell_counters(cell: int, n_periods: int) -> dict[str, np.ndarray]:
    """Draw synthetic counters for one cell (rule W1 seeding).

    Args:
        cell: Cell index, the rule W1 entity id.
        n_periods: Number of 15-minute periods.

    Returns:
        Arrays of RRC and E-RAB attempts and successes, one value per period.
    """
    rng = np.random.default_rng([BASE_SEED, PURPOSE_COUNTERS, cell])
    rrc_att = rng.poisson(MEAN_ATTEMPTS, n_periods)
    rrc_succ = rng.binomial(rrc_att, RRC_SUCCESS_P)
    erab_att = rrc_succ
    erab_succ = rng.binomial(erab_att, ERAB_SUCCESS_P)
    return {"rrc_att": rrc_att, "rrc_succ": rrc_succ, "erab_att": erab_att, "erab_succ": erab_succ}


def counters_table(rows: list[tuple[int, int, dict[str, int]]]) -> pa.Table:
    """Build an Arrow table of counter rows.

    Args:
        rows: (cell, period index, counters) triples.

    Returns:
        The rows with columns cell_id, period_start and the four counters.
    """
    names = ["rrc_att", "rrc_succ", "erab_att", "erab_succ"]
    return pa.table(
        {
            "cell_id": pa.array([f"CELL{c:04d}" for c, _, _ in rows], pa.string()),
            "period_start": pa.array([period_start(p) for _, p, _ in rows], pa.timestamp("us")),
            **{n: pa.array([v[n] for _, _, v in rows], pa.int64()) for n in names},
        }
    )


def synthetic_batches() -> tuple[pa.Table, pa.Table]:
    """Build the initial load and the late batch.

    The initial load lacks the late period for the first N_LATE_MISSING
    cells. The late batch carries those rows (inserts) plus corrected rows
    for the next N_LATE_RESENT cells (updates: attempts raised by one).

    Returns:
        The initial load and the late batch.
    """
    n_periods = N_DAYS * PERIODS_PER_DAY
    initial: list[tuple[int, int, dict[str, int]]] = []
    late: list[tuple[int, int, dict[str, int]]] = []
    for cell in range(N_CELLS):
        counters = cell_counters(cell, n_periods)
        for p in range(n_periods):
            values = {k: int(v[p]) for k, v in counters.items()}
            if p == LATE_PERIOD_INDEX and cell < N_LATE_MISSING:
                late.append((cell, p, values))
                continue
            initial.append((cell, p, values))
            if p == LATE_PERIOD_INDEX and cell < N_LATE_MISSING + N_LATE_RESENT:
                corrected = dict(values, rrc_att=values["rrc_att"] + 1)
                late.append((cell, p, corrected))
    return counters_table(initial), counters_table(late)


def timed(action: Callable[[], Any]) -> tuple[Any, float]:
    """Run an action and time it.

    Args:
        action: Zero-argument callable.

    Returns:
        The action's result and the elapsed seconds.
    """
    start = time.perf_counter()
    result = action()
    return result, time.perf_counter() - start


def attach(warehouse: str) -> duckdb.DuckDBPyConnection:
    """Open DuckDB with the warehouse attached as catalog `lk`.

    Args:
        warehouse: Lakekeeper warehouse name.

    Returns:
        The connection.
    """
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
    # AUTHORIZATION_TYPE 'none' matches the allowall catalog; S3 access uses
    # the STS credentials Lakekeeper vends (ACCESS_DELEGATION_MODE default).
    con.execute(
        f"ATTACH '{warehouse}' AS lk (TYPE iceberg, "
        f"ENDPOINT '{CATALOG_URL}/catalog', AUTHORIZATION_TYPE 'none')"
    )
    return con


def scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    """Run a query that returns one value.

    Args:
        con: DuckDB connection.
        sql: The query.

    Returns:
        The first column of the first row.

    Raises:
        CheckFailed: If the query returns no row.
    """
    row = con.execute(sql).fetchone()
    expect(row is not None, f"no row from: {sql}")
    assert row is not None
    return row[0]


def snapshot_log_length(warehouse: str, table: str) -> int:
    """Count snapshot-log entries (snapshots that became current) via PyIceberg.

    Args:
        warehouse: Lakekeeper warehouse name.
        table: Namespace-qualified table name.

    Returns:
        The number of snapshot-log entries.
    """
    return len(pyiceberg_catalog(warehouse).load_table(table).metadata.snapshot_log)


def pyiceberg_catalog(warehouse: str) -> Any:
    """Load the warehouse as a PyIceberg REST catalog.

    Args:
        warehouse: Lakekeeper warehouse name.

    Returns:
        The PyIceberg catalog.
    """
    return load_catalog("lk", type="rest", uri=f"{CATALOG_URL}/catalog", warehouse=warehouse)


def kpi_sql(version_id: int, source: str) -> str:
    """SQL for the daily accessibility KPI under one formula version.

    Args:
        version_id: Formula version, a key of FORMULAS.
        source: Fully qualified counters table.

    Returns:
        A SELECT producing cell_id, day, formula_version, value.
    """
    return (
        f"SELECT cell_id, CAST(period_start AS DATE) AS day, "
        f"{version_id} AS formula_version, {FORMULAS[version_id]} AS value "
        f"FROM {source} GROUP BY cell_id, CAST(period_start AS DATE)"
    )


def expected_kpi(
    initial: pa.Table, late: pa.Table, version_id: int
) -> dict[tuple[str, str], float]:
    """Compute the daily KPI in Python, independently of any engine.

    Args:
        initial: The initial load.
        late: The late batch (replaces or adds rows by key).
        version_id: Formula version.

    Returns:
        Value per (cell_id, ISO day).
    """
    rows: dict[tuple[str, datetime], dict[str, int]] = {}
    for batch in (initial, late):
        for r in batch.to_pylist():
            rows[(r["cell_id"], r["period_start"])] = r
    sums: dict[tuple[str, str], list[int]] = {}
    for (cell, start), r in rows.items():
        acc = sums.setdefault((cell, start.date().isoformat()), [0, 0, 0, 0])
        for i, name in enumerate(["rrc_att", "rrc_succ", "erab_att", "erab_succ"]):
            acc[i] += r[name]
    if version_id == 1:
        return {k: v[1] / v[0] for k, v in sums.items()}
    return {k: (v[1] / v[0]) * (v[3] / v[2]) for k, v in sums.items()}


def engine_kpi(
    con: duckdb.DuckDBPyConnection, table: str, version_id: int
) -> dict[tuple[str, str], float]:
    """Read one formula version of a gold KPI table.

    Args:
        con: DuckDB connection.
        table: Fully qualified KPI table.
        version_id: Formula version to read.

    Returns:
        Value per (cell_id, ISO day).
    """
    rows = con.execute(
        f"SELECT cell_id, CAST(day AS VARCHAR), value FROM {table} WHERE formula_version = ?",
        [version_id],
    ).fetchall()
    return {(c, d): float(v) for c, d, v in rows}


def same_kpi(got: dict[tuple[str, str], float], want: dict[tuple[str, str], float]) -> bool:
    """Compare two KPI series key by key within float tolerance.

    Args:
        got: Series from an engine.
        want: Independently computed series.

    Returns:
        True when keys match and every value agrees to 1e-12.
    """
    return got.keys() == want.keys() and all(abs(got[k] - want[k]) < 1e-12 for k in want)


class Runner:
    """Runs checks and records each one, keeping failures as data.

    Attributes:
        record: The record checks are appended to.
    """

    def __init__(self, record: Record) -> None:
        """Create a runner.

        Args:
            record: The record to append checks to.
        """
        self.record = record

    def run(self, engine: str, case: str, body: Callable[[], tuple[str, float]]) -> bool:
        """Run one check body and append its result.

        Expected failures of the stack under test (CheckFailed, DuckDB,
        subprocess and runtime errors) are recorded with their exact text;
        anything else propagates.

        Args:
            engine: Tool under test.
            case: What is checked.
            body: Returns (detail, seconds) or raises.

        Returns:
            Whether the check passed.
        """
        try:
            detail, seconds = body()
        except (CheckFailed, duckdb.Error, subprocess.CalledProcessError, RuntimeError) as exc:
            text = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                text = f"{exc}\n{(exc.stdout or '')[-3000:]}\n{(exc.stderr or '')[-3000:]}".strip()
            self.record.checks.append(Check(engine, case, False, 0.0, "", text))
            return False
        self.record.checks.append(Check(engine, case, True, round(seconds, 3), detail, ""))
        return True


def check_duckdb(runner: Runner, warehouse: str, initial: pa.Table, late: pa.Table) -> None:
    """DuckDB checks: write, late MERGE, time travel, formula change.

    Args:
        runner: Check runner.
        warehouse: Lakekeeper warehouse name.
        initial: The initial load.
        late: The late batch.
    """
    con = attach(warehouse)
    con.register("initial_load", initial)
    con.register("late_batch", late)
    before_merge: dict[str, Any] = {}

    def create_and_write() -> tuple[str, float]:
        con.execute("CREATE SCHEMA lk.ran")
        con.execute(
            "CREATE TABLE lk.ran.counters (cell_id VARCHAR, period_start TIMESTAMP, "
            "rrc_att BIGINT, rrc_succ BIGINT, erab_att BIGINT, erab_succ BIGINT)"
        )
        _, seconds = timed(
            lambda: con.execute("INSERT INTO lk.ran.counters SELECT * FROM initial_load")
        )
        count = scalar(con, "SELECT count(*) FROM lk.ran.counters")
        expect(count == initial.num_rows, f"expected {initial.num_rows} rows, got {count}")
        return f"{count} rows written in one INSERT", seconds

    def merge_late() -> tuple[str, float]:
        snaps = con.execute(
            "SELECT snapshot_id, timestamp_ms FROM iceberg_snapshots(lk.ran.counters) "
            "ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        assert snaps is not None
        before_merge["snapshot_id"], before_merge["timestamp"] = snaps
        log_before = snapshot_log_length(warehouse, "ran.counters")
        _, seconds = timed(
            lambda: con.execute(
                "MERGE INTO lk.ran.counters AS t USING late_batch AS s "
                "ON t.cell_id = s.cell_id AND t.period_start = s.period_start "
                "WHEN MATCHED THEN UPDATE SET rrc_att = s.rrc_att, rrc_succ = s.rrc_succ, "
                "erab_att = s.erab_att, erab_succ = s.erab_succ "
                "WHEN NOT MATCHED THEN INSERT VALUES "
                "(s.cell_id, s.period_start, s.rrc_att, s.rrc_succ, s.erab_att, s.erab_succ)"
            )
        )
        count = scalar(con, "SELECT count(*) FROM lk.ran.counters")
        want = initial.num_rows + N_LATE_MISSING
        expect(count == want, f"expected {want} rows after MERGE, got {count}")
        mismatched = scalar(
            con,
            "SELECT count(*) FROM late_batch s LEFT JOIN lk.ran.counters t "
            "ON t.cell_id = s.cell_id AND t.period_start = s.period_start "
            "WHERE t.rrc_att IS DISTINCT FROM s.rrc_att",
        )
        expect(mismatched == 0, f"{mismatched} late rows not applied by MERGE")
        n_snaps = scalar(con, "SELECT count(*) FROM iceberg_snapshots(lk.ran.counters)")
        log_after = snapshot_log_length(warehouse, "ran.counters")
        expect(log_after == log_before + 1, f"snapshot log grew by {log_after - log_before}, not 1")
        runner.record.notes.append(
            f"DuckDB MERGE (update plus insert) wrote {n_snaps - 1} snapshots after the "
            f"initial load but moved the snapshot log by {log_after - log_before}: the "
            "intermediate snapshot never became current, so readers see the merge atomically."
        )
        return (
            f"{N_LATE_RESENT} rows updated and {N_LATE_MISSING} inserted; every late row "
            f"applied; {count} rows",
            seconds,
        )

    def time_travel() -> tuple[str, float]:
        sid = before_merge["snapshot_id"]
        ts = before_merge["timestamp"]
        (count, seconds) = timed(
            lambda: scalar(con, f"SELECT count(*) FROM lk.ran.counters AT (VERSION => {sid})")
        )
        expect(count == initial.num_rows, f"AT VERSION saw {count} rows, want {initial.num_rows}")
        count_ts = scalar(
            con, f"SELECT count(*) FROM lk.ran.counters AT (TIMESTAMP => TIMESTAMP '{ts}')"
        )
        expect(
            count_ts == initial.num_rows,
            f"AT TIMESTAMP saw {count_ts} rows, want {initial.num_rows}",
        )
        old_values = scalar(
            con,
            # DuckDB 1.5.6 rejects a table alias after the AT clause; a
            # subquery carries the alias instead.
            f"SELECT count(*) FROM (SELECT * FROM lk.ran.counters AT (VERSION => {sid})) AS t "
            "JOIN late_batch s "
            "ON t.cell_id = s.cell_id AND t.period_start = s.period_start "
            "WHERE t.rrc_att = s.rrc_att - 1",
        )
        expect(old_values == N_LATE_RESENT, f"pre-merge snapshot shows {old_values} old values")
        return (
            f"AT (VERSION) and AT (TIMESTAMP) return the pre-merge table: {count} rows, "
            f"{old_values} rows with their pre-correction values",
            seconds,
        )

    def formula_change() -> tuple[str, float]:
        con.execute(
            "CREATE TABLE lk.ran.kpi_formula "
            "(kpi_id VARCHAR, formula_version INTEGER, formula VARCHAR)"
        )
        con.execute(
            "CREATE TABLE lk.ran.kpi_accessibility_day (cell_id VARCHAR, day DATE, "
            "formula_version INTEGER, value DOUBLE)"
        )
        con.execute("INSERT INTO lk.ran.kpi_formula VALUES ('accessibility', 1, ?)", [FORMULAS[1]])
        con.execute(f"INSERT INTO lk.ran.kpi_accessibility_day {kpi_sql(1, 'lk.ran.counters')}")
        con.execute("INSERT INTO lk.ran.kpi_formula VALUES ('accessibility', 2, ?)", [FORMULAS[2]])
        _, seconds = timed(
            lambda: con.execute(
                f"INSERT INTO lk.ran.kpi_accessibility_day {kpi_sql(2, 'lk.ran.counters')}"
            )
        )
        for v in (1, 2):
            got = engine_kpi(con, "lk.ran.kpi_accessibility_day", v)
            expect(
                same_kpi(got, expected_kpi(initial, late, v)), f"formula version {v} values differ"
            )
        labelled = scalar(
            con,
            "SELECT count(*) FROM lk.ran.kpi_accessibility_day k JOIN lk.ran.kpi_formula f "
            "USING (formula_version)",
        )
        total = scalar(con, "SELECT count(*) FROM lk.ran.kpi_accessibility_day")
        expect(labelled == total, "some KPI rows carry no formula label")
        return (
            f"both formula versions stored side by side ({total} rows), each row joined to its "
            "formula text; reprocessing inserted version 2 for the full history",
            seconds,
        )

    if runner.run("duckdb", "create and write an Iceberg table", create_and_write):
        merged = runner.run("duckdb", "MERGE INTO for a late batch (rule D1)", merge_late)
        if merged:
            runner.run("duckdb", "time travel to the pre-merge snapshot (rule D7)", time_travel)
        runner.run("duckdb", "formula change and reprocess (rule D6)", formula_change)


def check_pyiceberg(runner: Runner, warehouse: str, initial: pa.Table) -> None:
    """PyIceberg reads the tables DuckDB wrote (rule S1).

    Args:
        runner: Check runner.
        warehouse: Lakekeeper warehouse name.
        initial: The initial load.
    """

    def read() -> tuple[str, float]:
        table = pyiceberg_catalog(warehouse).load_table("ran.counters")
        arrow, seconds = timed(lambda: table.scan().to_arrow())
        want = initial.num_rows + N_LATE_MISSING
        expect(arrow.num_rows == want, f"PyIceberg read {arrow.num_rows} rows, want {want}")
        first = min(table.metadata.snapshots, key=lambda s: s.sequence_number)
        old = table.scan(snapshot_id=first.snapshot_id).to_arrow()
        expect(old.num_rows == initial.num_rows, f"PyIceberg old snapshot has {old.num_rows} rows")
        kpi = pyiceberg_catalog(warehouse).load_table("ran.kpi_accessibility_day").scan().to_arrow()
        versions = sorted(set(kpi.column("formula_version").to_pylist()))
        expect(versions == [1, 2], f"PyIceberg sees formula versions {versions}")
        return (
            f"read the merged table ({arrow.num_rows} rows, delete files applied), the "
            f"first snapshot ({old.num_rows} rows) and both KPI formula versions",
            seconds,
        )

    runner.run("pyiceberg", "read the same tables (rule S1)", read)


def tool_env(warehouse: str, workdir: Path) -> dict[str, str]:
    """Environment for the dbt and SQLMesh subprocesses.

    Args:
        warehouse: Lakekeeper warehouse name.
        workdir: Scratch directory for the tool's local DuckDB file and output.

    Returns:
        The current environment with the run settings added.
    """
    return dict(
        os.environ,
        STACKCHECK_WAREHOUSE=warehouse,
        STACKCHECK_DUCKDB_PATH=str(workdir / "local.duckdb"),
        DBT_TARGET_PATH=str(workdir / "target"),
        DBT_LOG_PATH=str(workdir / "logs"),
        NO_COLOR="1",
        DBT_SEND_ANONYMOUS_USAGE_STATS="false",
    )


def error_lines(output: str) -> str:
    """Pick the lines that state an error from a tool's output.

    Args:
        output: Combined stdout and stderr.

    Returns:
        The distinct error lines in order, or the last lines when none match.
    """
    clean = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in output.splitlines()]
    picked: list[str] = []
    for line in clean:
        if ("Error" in line or "error:" in line) and line not in picked:
            picked.append(line)
    return "\n".join(picked[-6:] if picked else clean[-6:])


def run_tool(args: list[str], cwd: Path, env: dict[str, str]) -> float:
    """Run a tool command and time it.

    Args:
        args: Command and arguments; the first is a script in this venv.
        cwd: Working directory.
        env: Environment.

    Returns:
        Elapsed seconds.

    Raises:
        RuntimeError: With the tool's error lines when it exits non-zero.
    """
    command = [str(Path(sys.executable).parent / args[0]), *args[1:]]
    start = time.perf_counter()
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    seconds = time.perf_counter() - start
    if done.returncode != 0:
        raise RuntimeError(error_lines(done.stdout + "\n" + done.stderr))
    return seconds


def check_dbt(runner: Runner, warehouse: str, initial: pa.Table, late: pa.Table) -> None:
    """dbt-duckdb checks: the rule D6 case as an incremental and as a table model.

    Args:
        runner: Check runner.
        warehouse: Lakekeeper warehouse name.
        initial: The initial load.
        late: The late batch.
    """
    project = TOOLS_DIR / "dbt"

    def dbt(workdir: Path, select: str, formula_version: int) -> float:
        return run_tool(
            [
                "dbt",
                "run",
                "--profiles-dir",
                str(project),
                "--select",
                select,
                "--vars",
                f"{{formula_version: {formula_version}}}",
            ],
            project,
            tool_env(warehouse, workdir),
        )

    def incremental() -> tuple[str, float]:
        with tempfile.TemporaryDirectory() as tmp:
            dbt(Path(tmp), "kpi_accessibility_day", 1)
            seconds = dbt(Path(tmp), "kpi_accessibility_day", 2)
        con = attach(warehouse)
        for v in (1, 2):
            got = engine_kpi(con, "lk.dbt_gold.kpi_accessibility_day", v)
            expect(
                same_kpi(got, expected_kpi(initial, late, v)), f"formula version {v} values differ"
            )
        total = scalar(con, "SELECT count(*) FROM lk.dbt_gold.kpi_accessibility_day")
        return (
            f"version 1 then version 2 merged into one Iceberg table keyed by formula_version "
            f"({total} rows); both match the independent computation",
            seconds,
        )

    def table_model() -> tuple[str, float]:
        with tempfile.TemporaryDirectory() as tmp:
            seconds = dbt(Path(tmp), "kpi_accessibility_day_table", 1)
            seconds += dbt(Path(tmp), "kpi_accessibility_day_table", 2)
        return "table model built, then rebuilt with the new formula", seconds

    runner.run("dbt", "incremental merge model, formula change (rule D6)", incremental)
    runner.run("dbt", "table model rebuilt with a new formula (rule D6)", table_model)


def check_sqlmesh(runner: Runner, warehouse: str, initial: pa.Table, late: pa.Table) -> None:
    """SQLMesh checks: plan into Iceberg, then change the formula and backfill.

    The project is copied to a scratch directory so the formula edit never
    touches the committed model.

    Args:
        runner: Check runner.
        warehouse: Lakekeeper warehouse name.
        initial: The initial load.
        late: The late batch.
    """
    formula_line = "1 AS formula_version, sum(rrc_succ) / sum(rrc_att) AS value -- formula"
    new_line = f"2 AS formula_version, {FORMULAS[2]} AS value -- formula"

    def plan_and_change() -> tuple[str, float]:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            project = workdir / "project"
            shutil.copytree(TOOLS_DIR / "sqlmesh", project)
            env = tool_env(warehouse, workdir)
            plan = ["sqlmesh", "plan", "--auto-apply", "--no-prompts"]
            run_tool(plan, project, env)
            model = project / "models" / "kpi_accessibility_day.sql"
            text = model.read_text()
            expect(formula_line in text, "formula line not found in the SQLMesh model")
            model.write_text(text.replace(formula_line, new_line))
            seconds = run_tool(plan, project, env)
        con = attach(warehouse)
        tables = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'lk' AND table_schema = 'sqlmesh__sqlmesh_gold' "
                "ORDER BY table_name"
            ).fetchall()
        ]
        expect(len(tables) == 2, f"expected two physical versions, found {tables}")
        found = {}
        for name in tables:
            v = scalar(con, f'SELECT max(formula_version) FROM lk.sqlmesh__sqlmesh_gold."{name}"')
            got = engine_kpi(con, f'lk.sqlmesh__sqlmesh_gold."{name}"', v)
            expect(
                same_kpi(got, expected_kpi(initial, late, v)), f"formula version {v} values differ"
            )
            found[v] = name
        expect(sorted(found) == [1, 2], f"physical versions carry formula versions {sorted(found)}")
        return (
            "breaking change backfilled into a new physical Iceberg table; the old version stays "
            "queryable under its fingerprinted name",
            seconds,
        )

    runner.run(
        "sqlmesh", "plan into Iceberg, formula change and backfill (rule D6)", plan_and_change
    )


def versions() -> dict[str, str]:
    """Collect the versions under test.

    Returns:
        Name to version.
    """
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
    ext = dict(
        con.execute(
            "SELECT extension_name, extension_version FROM duckdb_extensions() "
            "WHERE extension_name IN ('iceberg', 'httpfs', 'avro')"
        ).fetchall()
    )
    names = ["duckdb", "pyiceberg", "pyarrow", "dbt-core", "dbt-duckdb", "sqlmesh"]
    found = {n: version(n) for n in names}
    found.update({f"duckdb extension {k}": v for k, v in sorted(ext.items())})
    info = http_json("GET", "/management/v1/info", None)
    found["lakekeeper"] = info["version"]
    found.update(compose_images())
    return found


def compose_images() -> dict[str, str]:
    """Read the pinned service images from compose.yaml.

    Returns:
        Service image references.
    """
    images = {}
    for line in (REPO_ROOT / "compose.yaml").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("image:"):
            ref = stripped.split(":", 1)[1].strip()
            images[f"image {ref.rsplit(':', 1)[0]}"] = ref.rsplit(":", 1)[1]
    return images


def main() -> int:
    """Run every check against the local stack and write the report.

    Returns:
        The process exit code: 0 when the report was written.
    """
    ensure_bootstrapped()
    warehouse = f"stackcheck-{uuid.uuid4().hex[:8]}"
    create_warehouse(warehouse)
    initial, late = synthetic_batches()
    record = Record(
        run_date=datetime.now(UTC).date().isoformat(),
        versions=versions(),
        data={
            "cells": N_CELLS,
            "days": N_DAYS,
            "initial_rows": initial.num_rows,
            "late_rows_inserted": N_LATE_MISSING,
            "late_rows_updated": N_LATE_RESENT,
        },
    )
    runner = Runner(record)
    check_duckdb(runner, warehouse, initial, late)
    check_pyiceberg(runner, warehouse, initial)
    check_dbt(runner, warehouse, initial, late)
    check_sqlmesh(runner, warehouse, initial, late)
    record.recommendation = recommend(record)
    record.evidence = list(EVIDENCE)
    write_report(record)
    return 0


EVIDENCE = (
    "Lakekeeper v0.13.6 has no filesystem warehouse: its storage profiles are s3, adls, gcs "
    "and onelake (crates/lakekeeper/src/service/storage/mod.rs and the management OpenAPI at "
    "tag v0.13.6), and docs.lakekeeper.io/getting-started says a warehouse needs an external "
    "object store (S3, ADLS, GCS). Hence SeaweedFS in compose.yaml (rule S3).",
    "MinIO is not used: github.com/minio/minio is archived and its README says the repository "
    "is no longer maintained; Lakekeeper's own compose examples moved to SeaweedFS "
    "(lakekeeper/lakekeeper pull request 1811, merged 2026-06-03).",
    "dbt-duckdb table materialization on Iceberg: CTAS into __dbt_tmp then rename in one "
    "transaction is refused by DuckDB-Iceberg; the fix is open upstream "
    "(duckdb/dbt-duckdb pull request 747).",
    "SQLMesh catalogs mapping accepts only type and path for an attached catalog "
    "(sqlmesh/core/config/connection.py at v0.236.2), so the Iceberg endpoint travels in an "
    "ICEBERG secret; DuckDB 1.5.6's iceberg extension (890b78a9c) cannot create views in an "
    "Iceberg catalog, so the virtual layer is mapped to a local DuckDB catalog.",
)


def outcome(record: Record, engine: str, case_prefix: str) -> bool:
    """Look up whether a check passed.

    Args:
        record: The run record.
        engine: Engine of the check.
        case_prefix: Start of the case text.

    Returns:
        Whether the check passed.

    Raises:
        KeyError: If no such check was recorded.
    """
    for c in record.checks:
        if c.engine == engine and c.case.startswith(case_prefix):
            return c.ok
    raise KeyError(f"no check {engine}: {case_prefix}")


def recommend(record: Record) -> list[str]:
    """Draw the recommendation from the measured outcomes.

    Only the outcome combination the text below was written for is
    accepted; any other result needs the conclusions rewritten.

    Args:
        record: The run record.

    Returns:
        The recommendation paragraphs.

    Raises:
        RuntimeError: If the outcomes differ from the ones the text covers.
    """
    observed = {
        "duckdb_merge": outcome(record, "duckdb", "MERGE"),
        "duckdb_time_travel": outcome(record, "duckdb", "time travel"),
        "duckdb_d6": outcome(record, "duckdb", "formula change"),
        "pyiceberg": outcome(record, "pyiceberg", "read"),
        "dbt_incremental": outcome(record, "dbt", "incremental"),
        "dbt_table": outcome(record, "dbt", "table model"),
        "sqlmesh": outcome(record, "sqlmesh", "plan"),
    }
    covered = {
        "duckdb_merge": True,
        "duckdb_time_travel": True,
        "duckdb_d6": True,
        "pyiceberg": True,
        "dbt_incremental": True,
        "dbt_table": False,
        "sqlmesh": False,
    }
    if observed != covered:
        raise RuntimeError(f"outcomes {observed} differ from the written conclusions")
    v = record.versions
    return [
        "No fallback needed. DuckDB-Iceberg MERGE and time travel both work through Lakekeeper, "
        "so bronze, silver and gold can all be Iceberg tables; the rule S2 fallback (bronze and "
        "silver in plain Parquet) does not apply.",
        f"Transform tool: dbt (dbt-core {v['dbt-core']}, dbt-duckdb {v['dbt-duckdb']}), with "
        "gold models materialized as incremental models using the merge strategy and a "
        "formula_version key. That pattern passed rule D6: a formula change is merged in beside "
        "the old version, and both stay queryable and labelled. Table materialization fails on "
        "Iceberg, so it is not used.",
        f"SQLMesh {v['sqlmesh']} is not usable on this stack as released: its DuckDB adapter "
        "switches catalogs with USE, which DuckDB-Iceberg rejects for an Iceberg catalog. Its "
        "versioned physical tables would suit rule D6, but it would need adapter changes.",
        "Late arrivals (rule D1) load through plain DuckDB MERGE INTO from "
        "Python: one MERGE moves the snapshot log once, so readers never see a half-applied "
        "batch. Rule D7 reads use AT (VERSION => ...) or AT (TIMESTAMP => ...); DuckDB 1.5.6 "
        "needs a subquery to alias a time-travel read.",
        "The timings are for a small synthetic batch and one run each; they show the operations "
        "work at interactive speed, not throughput. The dbt time is dominated by dbt start-up. "
        "Throughput is measured by the scale test (rule E1).",
    ]


def write_report(record: Record) -> None:
    """Write the record as JSON and its rendered Markdown.

    Args:
        record: The run record.
    """
    RESULTS_JSON.parent.mkdir(exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(asdict(record), indent=2) + "\n")
    RESULTS_MD.write_text(render_markdown(json.loads(RESULTS_JSON.read_text())))


def render_markdown(record: dict[str, Any]) -> str:
    """Render the report from the JSON record.

    Args:
        record: The record as loaded from JSON.

    Returns:
        The Markdown report.
    """
    lines = [
        "# Stack check: DuckDB, dbt and SQLMesh on Iceberg",
        "",
        "Synthetic data. Measured by `python -m ran_lakehouse.stackcheck` against the local",
        f"compose stack on {record['run_date']} UTC (rule S2). Rendered from `stack_spike.json`.",
        "",
        "## Versions",
        "",
        "| Component | Version |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in record["versions"].items()]
    lines += ["", "## Data", "", "| Item | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in record["data"].items()]
    lines += [
        "",
        "## Checks",
        "",
        "| Engine | Case | Result | Seconds | Detail |",
        "|---|---|---|---|---|",
    ]
    for c in record["checks"]:
        result = "pass" if c["ok"] else "FAIL"
        seconds = f"{c['seconds']:.3f}" if c["ok"] else "-"
        detail = c["detail"] if c["ok"] else "see errors"
        lines.append(f"| {c['engine']} | {c['case']} | {result} | {seconds} | {detail} |")
    failures = [c for c in record["checks"] if not c["ok"]]
    if failures:
        lines += ["", "## Errors", ""]
        for c in failures:
            lines += [f"{c['engine']}, {c['case']}:", "", "```", c["error"], "```", ""]
        lines.pop()
    if record["notes"]:
        lines += ["", "## Notes", ""]
        lines += [f"- {n}" for n in record["notes"]]
    lines += ["", "## Recommendation", ""]
    lines += [f"- {n}" for n in record["recommendation"]]
    lines += ["", "## Evidence", ""]
    lines += [f"- {n}" for n in record["evidence"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
