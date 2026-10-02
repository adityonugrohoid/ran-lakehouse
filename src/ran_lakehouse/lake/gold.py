"""Gold (rules L3, L5, D6): KPIs from silver through dbt, one UTC day at a time.

The dbt project (transform/) holds incremental merge models only, keyed with
the formula version (rule S2). For each UTC day that silver has built for
every EMS and gold has not yet seen (including days a late file was merged
into), dbt merges that day's cell counters and 15-minute and hourly KPIs,
and recomputes the WIB days and weeks the day touches, and the week's
worst-cell ranking. Merge predicates keep each MERGE to the partitions of
its window.

Rule D6: on the revision day the operator revises one KPI. When the build
reaches it, history is reprocessed from the gold cell counters (themselves
from silver) for the new version only, and from then on both versions are
computed; both stay queryable, labelled by formula_version and in the
catalog. The revision is the planted answer in evaluation.kpi_revisions.
"""

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.lake.bronze import EVALUATION
from ran_lakehouse.lake.catalog import (
    COMMIT_ATTEMPTS,
    COMMIT_CONFLICT,
    COMMIT_RETRY_WAIT_S,
    DUCKDB_MEMORY_LIMIT,
    connect,
    write,
)
from ran_lakehouse.lake.kpi_catalog import INPUTS, KPIS, Kpi, catalog_table, revised
from ran_lakehouse.lake.silver import LOADS as SILVER_LOADS
from ran_lakehouse.lake.silver import scalar
from ran_lakehouse.model import RUN_START

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_DIR = REPO_ROOT / "transform"
DBT_WORK = REPO_ROOT / "data" / "dbt"
GOLD = "lk.gold"
CATALOG = f"{GOLD}.kpi_catalog"
LOADS = f"{GOLD}.loads"
REVISIONS = f"{EVALUATION}.kpi_revisions"
WIB = timedelta(hours=7)  # rule W5
PERSISTENCE_N = 3  # breach days of a week for a persistent worst cell (START, rule L5)
PERSISTENCE_M = 7  # days in the window (a WIB week)
REPROCESS_DAYS = 7  # UTC days per dbt run when reprocessing history (rule D6)
MIN_COVERAGE = 0.75  # a day is judged only with this share of its periods (START)

TABLES = {
    CATALOG: """(
        kpi_id VARCHAR, formula_version INTEGER, name VARCHAR, technology VARCHAR,
        formula VARCHAR, source VARCHAR, unit VARCHAR, granularities VARCHAR,
        vendors VARCHAR, better VARCHAR, breach_threshold DOUBLE, effective_from DATE,
        operators_differ VARCHAR)""",
    LOADS: """(
        load_id VARCHAR, kind VARCHAR, utc_day DATE, silver_kind VARCHAR, silver_ems VARCHAR,
        silver_window TIMESTAMPTZ, seconds DOUBLE)""",
    REVISIONS: """(
        kpi_id VARCHAR, from_version INTEGER, to_version INTEGER, effective_day DATE,
        detail VARCHAR)""",
}
# A dbt parse during a wall-clock step (see lake.catalog.write) can record a
# model's refs incompletely; the run then fails with this message before
# writing anything, and is run again like a rejected commit.
DBT_PARSE_MISS = "unable to infer all dependencies"
KPI_MODELS = [f"{t}_kpi_{g}" for t in ("lte", "gsm") for g in ("15m", "hour", "day", "week")]


@dataclass(frozen=True)
class Revision:
    """A planted KPI formula revision (rule D6).

    Attributes:
        kpi_id: The KPI revised.
        version: Its new formula version.
        effective_day: First WIB day the new version is current.
    """

    kpi_id: str
    version: int
    effective_day: date


# Week 9, Monday (START): the operator's revision of the RRC setup success
# rate to count a set-up only when S1 signalling also succeeds.
KPI_REVISION = Revision("LTE_RRC_SSR", 2, (RUN_START + timedelta(days=56)).date())


@dataclass(frozen=True)
class Target:
    """Where the lake is.

    Attributes:
        kind: "lake" (a Lakekeeper warehouse) or "file" (a DuckDB file with
            the same schemas, for tests).
        location: Warehouse name or file path.
    """

    kind: str
    location: str

    def connect(self) -> duckdb.DuckDBPyConnection:
        """DuckDB with the lake attached as "lk".

        Returns:
            The connection.

        Raises:
            ValueError: For an unknown kind.
        """
        if self.kind == "lake":
            return connect(self.location)
        if self.kind == "file":
            con = duckdb.connect()
            con.execute("SET TimeZone = 'UTC'")
            con.execute(f"ATTACH '{self.location}' AS lk")
            return con
        raise ValueError(f"unknown target kind {self.kind}")

    def environment(self) -> dict[str, str]:
        """Environment for the dbt profile (transform/profiles.yml).

        Returns:
            Variables to set.
        """
        return {
            "RANLAKE_WAREHOUSE": self.location if self.kind == "lake" else "unused",
            "RANLAKE_DUCKDB_FILE": self.location if self.kind == "file" else "unused",
            "RANLAKE_MEMORY_LIMIT": DUCKDB_MEMORY_LIMIT,
            # A file, not ":memory:": dbt-duckdb keeps an in-memory database
            # (and the attached lake) open for the whole process otherwise.
            "RANLAKE_DBT_DATABASE": str(DBT_WORK / "dbt.duckdb"),
            "DBT_TARGET_PATH": str(DBT_WORK / "target"),
            "DBT_LOG_PATH": str(DBT_WORK / "logs"),
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        }


def utc(day: date) -> datetime:
    """Midnight UTC of a date.

    Args:
        day: The date.

    Returns:
        Aware time.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def monday(day: date) -> date:
    """The Monday of a date's week.

    Args:
        day: The date.

    Returns:
        The Monday.
    """
    return day - timedelta(days=day.weekday())


def window_vars(first_utc_day: date, last_utc_day: date) -> dict[str, str]:
    """dbt vars for building UTC days first..last (inclusive).

    Args:
        first_utc_day: First UTC day.
        last_utc_day: Last UTC day.

    Returns:
        day_start/day_end (UTC), the WIB days the UTC days touch
        (local_first/local_last, local_start/local_end in UTC) and their
        WIB weeks (week_first/week_last, week_start/week_end in UTC).
    """
    day_start = utc(first_utc_day)
    day_end = utc(last_utc_day) + timedelta(days=1)
    local_first = (day_start + WIB).date()
    local_last = (day_end - timedelta(microseconds=1) + WIB).date()
    week_first, week_last = monday(local_first), monday(local_last)
    return {
        "day_start": day_start.isoformat(),
        "day_end": day_end.isoformat(),
        "local_first": local_first.isoformat(),
        "local_last": local_last.isoformat(),
        "local_start": (utc(local_first) - WIB).isoformat(),
        "local_end": (utc(local_last) + timedelta(days=1) - WIB).isoformat(),
        "week_first": week_first.isoformat(),
        "week_last": week_last.isoformat(),
        "week_start": (utc(week_first) - WIB).isoformat(),
        "week_end": (utc(week_last) + timedelta(days=7) - WIB).isoformat(),
    }


class GoldBuild:
    """Builds gold from silver through dbt."""

    def __init__(self, target: Target, load_id: str, revision: Revision) -> None:
        """Prepare a build.

        Args:
            target: The lake.
            load_id: Id of this gold run.
            revision: The planted formula revision.
        """
        self.target = target
        self.load_id = load_id
        self.revision = revision
        self.partitioned = target.kind == "lake"
        self.stats: dict[str, Any] = {
            "days": 0,
            "dbt_runs": 0,
            "dbt_retries": 0,
            "reprocess_s": 0.0,
            "days_s": 0.0,
        }

    def run(self) -> dict[str, Any]:
        """Build every UTC day silver has finished that gold has not seen.

        Returns:
            Counts and timings.
        """
        con = self.target.connect()
        self.create_tables(con)
        pending = self.pending(con)
        applied = scalar(con, f"SELECT count(*) FROM {REVISIONS}") > 0
        self.write_catalog(con, applied)
        con.close()
        for day, silver_loads in pending:
            if not applied and window_vars(day, day)["local_last"] >= str(
                self.revision.effective_day
            ):
                self.reprocess(day)
                applied = True
            started = time.perf_counter()
            versions = {self.revision.kpi_id: [1, self.revision.version]} if applied else {}
            self.dbt(window_vars(day, day), versions, "", [])
            seconds = time.perf_counter() - started
            self.stats["days"] += 1
            self.stats["days_s"] += seconds
            self.record("day", day, silver_loads, seconds)
        return self.stats

    def create_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create the gold schema and the tables Python writes.

        Args:
            con: DuckDB with the lake attached.
        """
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {GOLD}")
        existing = {
            f"lk.{schema}.{name}"
            for schema, name in con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_catalog = 'lk'"
            ).fetchall()
        }
        for table, columns in TABLES.items():
            if table not in existing:
                con.execute(f"CREATE TABLE {table} {columns}")

    def pending(self, con: duckdb.DuckDBPyConnection) -> list[tuple[date, list[tuple[Any, ...]]]]:
        """UTC days to build, with the silver loads they answer.

        A day is built once silver has its partition for every EMS; a
        silver catch-up load (a late file) makes its day pending again.

        Args:
            con: DuckDB with the lake attached.

        Returns:
            (UTC day, silver loads (kind, ems, window_start)) in day order.
        """
        silver = con.execute(f"SELECT kind, ems, window_start FROM {SILVER_LOADS}").fetchall()
        seen = set(
            con.execute(
                f"SELECT silver_kind, silver_ems, silver_window FROM {LOADS} WHERE kind = 'day'"
            ).fetchall()
        )
        complete: dict[date, set[str]] = {}
        for kind, ems, window in silver:
            if kind == "partition":
                complete.setdefault(window.astimezone(UTC).date(), set()).add(ems)
        every = {e.ems_id for e in EMS_LIST}
        days: dict[date, list[tuple[Any, ...]]] = {}
        for load in silver:
            if tuple(load) in seen:
                continue
            day = load[2].astimezone(UTC).date()
            if complete.get(day) == every:
                days.setdefault(day, []).append(tuple(load))
        return sorted(days.items())

    def write_catalog(self, con: duckdb.DuckDBPyConnection, applied: bool) -> None:
        """Write the KPI catalog, with the revised version once applied.

        Args:
            con: DuckDB with the lake attached.
            applied: Whether the revision has been applied.
        """
        entries: tuple[Kpi, ...] = KPIS
        if applied:
            r = self.revision
            entries = (*KPIS, revised(r.kpi_id, r.version, r.effective_day))
        write(con, f"DELETE FROM {CATALOG}")
        con.register("new_catalog", catalog_table(entries))
        try:
            write(con, f"INSERT INTO {CATALOG} SELECT * FROM new_catalog")
        finally:
            con.unregister("new_catalog")

    def reprocess(self, day: date) -> None:
        """Apply the revision: rebuild history before a UTC day for the new version.

        Args:
            day: The first UTC day built with both versions.
        """
        started = time.perf_counter()
        r = self.revision
        con = self.target.connect()
        first = (
            scalar(
                con,
                f"SELECT min(window_start) FROM {SILVER_LOADS} WHERE kind = 'partition'",
            )
            .astimezone(UTC)
            .date()
        )
        self.write_catalog(con, True)
        write(
            con,
            f"INSERT INTO {REVISIONS} VALUES ('{r.kpi_id}', {r.version - 1}, {r.version}, "
            f"DATE '{r.effective_day.isoformat()}', "
            f"'history reprocessed from {first.isoformat()} before the build reached "
            f"UTC day {day.isoformat()}')",
        )
        con.close()
        self.rebuild_version(r.kpi_id, r.version, first, day - timedelta(days=1))
        seconds = time.perf_counter() - started
        self.stats["reprocess_s"] += seconds
        self.record("reprocess", day, [], seconds)

    def rebuild_version(self, kpi_id: str, version: int, first: date, last: date) -> None:
        """Rebuild one KPI formula version's gold values over UTC days first..last.

        The values come from the gold cell counters through dbt; every model
        merges, so a rebuild of values already there changes nothing.

        Args:
            kpi_id: KPI id.
            version: Formula version.
            first: First UTC day.
            last: Last UTC day (inclusive).
        """
        models = [m for m in KPI_MODELS if m.startswith(kpi_id.split("_")[0].lower())]
        # A week of UTC days per run keeps each MERGE within DuckDB's memory
        # limit; WIB days and weeks at a chunk's edge are merged twice.
        chunk = first
        while chunk <= last:
            end = min(chunk + timedelta(days=REPROCESS_DAYS - 1), last)
            self.dbt(
                window_vars(chunk, end),
                {kpi_id: [version]},
                kpi_id,
                [*models, "worst_cells_week"],
            )
            chunk = end + timedelta(days=1)

    def reprocess_range(self, kpi_id: str, version: int, first: date, last: date) -> dict[str, Any]:
        """Reprocess a KPI formula version over a UTC day range (rule S4, D6).

        Args:
            kpi_id: KPI id.
            version: Formula version.
            first: First UTC day.
            last: Last UTC day (inclusive).

        Returns:
            Counts and timings.

        Raises:
            ValueError: For an unknown KPI version, an empty range, or days
                gold has not built.
        """
        if (kpi_id, version) not in INPUTS:
            raise ValueError(f"no formula recorded for {kpi_id} version {version}")
        if last < first:
            raise ValueError(f"last day {last} is before first day {first}")
        con = self.target.connect()
        built = con.execute(
            f"SELECT min(utc_day), max(utc_day) FROM {LOADS} WHERE kind = 'day'"
        ).fetchone()
        con.close()
        if built is None or built[0] is None or first < built[0] or last > built[1]:
            raise ValueError(f"gold has built UTC days {built}; {first}..{last} is outside")
        started = time.perf_counter()
        self.rebuild_version(kpi_id, version, first, last)
        seconds = time.perf_counter() - started
        self.stats["reprocess_s"] += seconds
        self.record("reprocess", first, [], seconds)
        return self.stats

    def dbt(
        self,
        window: dict[str, str],
        versions: dict[str, list[int]],
        only_kpi: str,
        select: list[str],
    ) -> None:
        """Run the dbt project for one window.

        A run that failed after a wall-clock step (a commit the catalog
        rejected, see lake.catalog.write, or a parse that missed refs,
        DBT_PARSE_MISS) is run again: every model merges, so a rerun is
        idempotent. Each retry is logged and counted.

        Args:
            window: Window vars (window_vars()).
            versions: Formula versions per KPI (others: version 1).
            only_kpi: Restrict KPI models to one KPI, or "" for all.
            select: Models to run, or [] for all.

        Raises:
            RuntimeError: If the run fails for another reason, or keeps
                failing on commit conflicts.
        """
        variables = window | {
            "versions": versions,
            "only_kpi": only_kpi,
            "partitioned": self.partitioned,
            "persistence_n": PERSISTENCE_N,
            "persistence_m": PERSISTENCE_M,
            "min_coverage": MIN_COVERAGE,
        }
        args = [
            "run",
            "--quiet",
            "--no-use-colors",
            # Vars change every run, so a cached parse would be rebuilt anyway.
            "--no-partial-parse",
            "--project-dir",
            str(PROJECT_DIR),
            "--profiles-dir",
            str(PROJECT_DIR),
            "--target",
            self.target.kind,
            "--vars",
            json.dumps(variables),
        ]
        if select:
            args += ["--select", *select]
        DBT_WORK.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, COMMIT_ATTEMPTS + 1):
            self.stats["dbt_runs"] += 1
            # A process of its own: dbt-duckdb keeps its DuckDB, and the
            # attached lake, open for the life of the process that ran it.
            result = subprocess.run(
                [str(Path(sys.executable).parent / "dbt"), *args],
                env=os.environ | self.target.environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return
            messages = (result.stdout + result.stderr)[-4000:]
            transient = COMMIT_CONFLICT in messages or DBT_PARSE_MISS in messages
            if not transient or attempt == COMMIT_ATTEMPTS:
                raise RuntimeError(f"dbt run failed for {window['day_start']}: {messages}")
            self.stats["dbt_retries"] += 1
            logger.warning(
                "dbt run failed transiently (%s), attempt %d of %d; rerunning in %.1f s",
                COMMIT_CONFLICT if COMMIT_CONFLICT in messages else DBT_PARSE_MISS,
                attempt,
                COMMIT_ATTEMPTS,
                COMMIT_RETRY_WAIT_S,
            )
            time.sleep(COMMIT_RETRY_WAIT_S)

    def record(
        self, kind: str, day: date, silver_loads: list[tuple[Any, ...]], seconds: float
    ) -> None:
        """Record a gold load and the silver loads it answered.

        Args:
            kind: "day" or "reprocess".
            day: UTC day.
            silver_loads: (kind, ems, window_start) of silver loads.
            seconds: Time taken.
        """
        rows = silver_loads or [(None, None, None)]
        stamp = pa.timestamp("us", tz="UTC")
        table = pa.table(
            {
                "load_id": pa.array([self.load_id] * len(rows), pa.string()),
                "kind": pa.array([kind] * len(rows), pa.string()),
                "utc_day": pa.array([day] * len(rows), pa.date32()),
                "silver_kind": pa.array([r[0] for r in rows], pa.string()),
                "silver_ems": pa.array([r[1] for r in rows], pa.string()),
                "silver_window": pa.array([r[2] for r in rows], stamp),
                "seconds": pa.array([seconds] * len(rows), pa.float64()),
            }
        )
        con = self.target.connect()
        con.register("new_loads", table)
        try:
            write(con, f"INSERT INTO {LOADS} SELECT * FROM new_loads")
        finally:
            con.unregister("new_loads")
            con.close()
