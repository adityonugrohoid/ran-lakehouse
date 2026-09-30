"""Silver (rules L2, D1 to D5): typed PM measurements under 3GPP names.

Silver is built from bronze one partition at a time, a UTC day of one EMS,
once the day's cutoff (the day's end plus GRACE) has passed. It reads only
the bronze rows of files that had arrived by the cutoff. A file that
arrives after its partition was built is merged later (DuckDB MERGE into
the Iceberg table) into the hour it belongs to, and nothing else.

Per partition:
- mapping (rule D5): every vendor counter is mapped by vendor, software
  release and measInfo to a 3GPP measurement name (TS 32.425 for LTE,
  TS 52.402 for GSM) or, where no 3GPP measurement matches, to a labelled
  vendor-style quantity; a counter with no mapping fails the load;
- units: values are converted to the 3GPP unit (Huawei-style bits and
  Nokia-style kByte to kbit; Huawei-style used PRBs to the RRU.PrbTotDl
  percentage; Nokia-style availability samples to
  RRU.CellUnavailableTime.sum seconds); converted rows are marked derived;
- duplicates (rule D2): a same-content redelivery was skipped by the
  collector and is counted per file; for a file delivered again with
  different content, the latest version wins as a whole and the values it
  changed are flagged as a conflict;
- late files (rule D1): a file that arrived after a file of a later period
  is flagged late;
- gaps (rule D3): an expected network element with no values in a period is
  recorded in pm_gaps; silver has no row for it, never a zero;
- suspect data (rule D4): the TS 32.432 suspect flag is carried per value;
- every load runs its quality checks and fails loudly when one breaks.

Time is UTC throughout (bronze already is).
"""

import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import pyarrow as pa

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.files import omes, pm_xml
from ran_lakehouse.files.dialects import RELEASES, Dialect, Entry
from ran_lakehouse.files.ems import Ems
from ran_lakehouse.lake.bronze import BRONZE
from ran_lakehouse.lake.catalog import write

SILVER = "lk.silver"
MEASUREMENTS = f"{SILVER}.pm_measurements"
FILES = f"{SILVER}.pm_files"
GAPS = f"{SILVER}.pm_gaps"
MAP = f"{SILVER}.counter_map"
LOADS = f"{SILVER}.loads"
# A UTC day is built 30 minutes after it ends, once its last files are in
# (ASSUMPTION: normal delivery takes 2 to 8 minutes, rule P7).
GRACE = timedelta(minutes=30)
NO_BIN = -1

TABLES = {
    MEASUREMENTS: """(
        period_start TIMESTAMPTZ, period_end TIMESTAMPTZ, granularity_min INTEGER,
        ems VARCHAR, vendor VARCHAR, managed_element VARCHAR, object_dn VARCHAR,
        measurement VARCHAR, bin INTEGER, value DOUBLE, derived BOOLEAN, suspect BOOLEAN,
        late BOOLEAN, versions INTEGER, conflict BOOLEAN, dictionary_release VARCHAR,
        vendor_counter VARCHAR, file_name VARCHAR, file_hash VARCHAR,
        arrival_time TIMESTAMPTZ, load_id VARCHAR)""",
    FILES: """(
        ems VARCHAR, file_name VARCHAR, period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
        first_arrival TIMESTAMPTZ, deliveries INTEGER, versions INTEGER,
        latest_hash VARCHAR, late BOOLEAN, load_id VARCHAR)""",
    GAPS: """(
        ems VARCHAR, managed_element VARCHAR, period_start TIMESTAMPTZ,
        period_end TIMESTAMPTZ, granularity_min INTEGER, load_id VARCHAR)""",
    MAP: """(
        vendor VARCHAR, release VARCHAR, meas_group VARCHAR, vendor_counter VARCHAR,
        rule VARCHAR, measurement VARCHAR, bin INTEGER, factor DOUBLE, attestation VARCHAR)""",
    LOADS: """(
        load_id VARCHAR, kind VARCHAR, ems VARCHAR, window_start TIMESTAMPTZ,
        window_end TIMESTAMPTZ, cutoff TIMESTAMPTZ, files BIGINT, bronze_rows BIGINT,
        silver_rows BIGINT, derived_rows BIGINT, suspect_rows BIGINT, conflict_rows BIGINT,
        late_rows BIGINT, gaps BIGINT, seconds DOUBLE)""",
}
PARTITIONS = {MEASUREMENTS: "day(period_start)", GAPS: "day(period_start)"}

# Vendor-style quantities with no 3GPP measurement of their own, and the
# 3GPP measurements silver derives from them.
PRB_USED = "DL PRBs used, mean (vendor-style)"
PRB_AVAIL = "DL PRBs available (vendor-style)"
SAMPLES_AVAILABLE = "cell available samples (vendor-style)"
SAMPLES_TOTAL = "availability samples per period (vendor-style)"
PRB_PERCENT = "RRU.PrbTotDl"  # TS 32.425 clause 4.5.1: integer percentage
UNAVAILABLE_TIME = "RRU.CellUnavailableTime.sum"  # TS 32.425: seconds


def mapped_measurement(entry: Entry) -> tuple[str, float]:
    """The silver measurement of a vendor counter and the factor to its unit.

    Args:
        entry: The dictionary entry.

    Returns:
        (measurement name, factor from the vendor value to silver's value).

    Raises:
        ValueError: For an unknown rule.
    """
    if entry.rule == "copy":
        return entry.canonical[0], 1.0 / entry.scale
    if entry.rule == "bin":
        return entry.canonical[0], 1.0
    if entry.rule == "sum":
        return " + ".join(entry.canonical), 1.0
    if entry.rule == "difference":
        return f"{' - '.join(entry.canonical)}, not below 0 (vendor-style)", 1.0
    if entry.rule in ("relation_same_site", "relation_other_site"):
        where = "same-site" if entry.rule == "relation_same_site" else "other-site"
        return f"{entry.canonical[0]} over {where} targets (vendor-style)", 1.0
    labels = {
        "prb_used": PRB_USED,
        "prb_avail": PRB_AVAIL,
        "availability_samples": SAMPLES_AVAILABLE,
        "samples_total": SAMPLES_TOTAL,
    }
    if entry.rule in labels:
        return labels[entry.rule], 1.0
    raise ValueError(f"no silver mapping for rule {entry.rule}")


def counter_map() -> pa.Table:
    """The mapping of every vendor counter of every release (rule D5).

    Returns:
        Rows of silver.counter_map.
    """
    rows = []
    for dialect in RELEASES.values():
        for group in dialect.groups:
            for entry in group.entries:
                measurement, factor = mapped_measurement(entry)
                rows.append(
                    {
                        "vendor": dialect.vendor,
                        "release": dialect.release,
                        "meas_group": group.meas_info_id,
                        "vendor_counter": entry.name,
                        "rule": entry.rule,
                        "measurement": measurement,
                        "bin": entry.bin_index if entry.rule == "bin" else NO_BIN,
                        "factor": factor,
                        "attestation": entry.attestation,
                    }
                )
    return pa.Table.from_pylist(rows).cast(
        pa.schema(
            [
                ("vendor", pa.string()),
                ("release", pa.string()),
                ("meas_group", pa.string()),
                ("vendor_counter", pa.string()),
                ("rule", pa.string()),
                ("measurement", pa.string()),
                ("bin", pa.int32()),
                ("factor", pa.float64()),
                ("attestation", pa.string()),
            ]
        )
    )


def create_tables(con: duckdb.DuckDBPyConnection, partitioned: bool) -> None:
    """Create the silver tables if missing, and write the counter map.

    Args:
        con: DuckDB with the warehouse attached as "lk".
        partitioned: Set Iceberg partitioning (False for a plain DuckDB
            catalog, which has none).
    """
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SILVER}")
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
        if partitioned and table in PARTITIONS:
            con.execute(f"ALTER TABLE {table} SET PARTITIONED BY ({PARTITIONS[table]})")
    if MAP not in existing:
        con.register("new_map", counter_map())
        try:
            write(con, f"INSERT INTO {MAP} SELECT * FROM new_map")
        finally:
            con.unregister("new_map")


def ts(t: datetime) -> str:
    """A TIMESTAMPTZ literal.

    Args:
        t: Aware time.

    Returns:
        SQL literal in UTC.
    """
    return f"TIMESTAMPTZ '{t.astimezone(UTC).isoformat()}'"


@dataclass(frozen=True)
class FileState:
    """What silver knows about one PM file at a cutoff.

    Attributes:
        ems: EMS name.
        file_name: File name.
        period_start: Start of the file's 15-minute period (UTC).
        period_end: End of the file's 15-minute period (UTC).
        first_arrival: First delivery.
        deliveries: Deliveries, including same-content redeliveries.
        versions: Distinct contents loaded.
        latest_hash: Hash of the latest content loaded (the winning version).
        late: Arrived after a file of a later period of the same EMS.
    """

    ems: str
    file_name: str
    period_start: datetime
    period_end: datetime
    first_arrival: datetime
    deliveries: int
    versions: int
    latest_hash: str
    late: bool


def file_period(name: str) -> tuple[datetime, datetime]:
    """The 15-minute period of a PM file, from its name.

    Args:
        name: File name (3GPP type A or B, or Nokia-style OMeS).

    Returns:
        UTC start and end.
    """
    if name.startswith("OMeS_"):
        begin = omes.parse_file_name(name)["begin"]
        return begin, begin + timedelta(minutes=15)
    parsed = pm_xml.parse_file_name(name)
    return parsed["begin"].astimezone(UTC), parsed["end"].astimezone(UTC)


def file_states(arrivals: list[tuple[Any, ...]], cutoff: datetime) -> dict[str, list[FileState]]:
    """File states per EMS from the arrivals known at a cutoff.

    Args:
        arrivals: (ems, file_name, file_hash, arrival_time, loaded) of PM
            deliveries.
        cutoff: Aware cutoff; later arrivals are not known yet.

    Returns:
        EMS to its files in period order.
    """
    seen: dict[tuple[str, str], list[tuple[datetime, str, bool]]] = defaultdict(list)
    for ems, name, digest, arrival, loaded in arrivals:
        if arrival <= cutoff:
            seen[(ems, name)].append((arrival, digest, loaded))
    per_ems: dict[str, list[FileState]] = defaultdict(list)
    for (ems, name), deliveries in seen.items():
        deliveries.sort()
        loaded = [(a, d) for a, d, ok in deliveries if ok]
        if not loaded:
            raise RuntimeError(f"{ems} {name}: delivered but never loaded into bronze")
        start, end = file_period(name)
        per_ems[ems].append(
            FileState(
                ems=ems,
                file_name=name,
                period_start=start,
                period_end=end,
                first_arrival=deliveries[0][0],
                deliveries=len(deliveries),
                versions=len({d for _, d in loaded}),
                latest_hash=loaded[-1][1],
                late=False,
            )
        )
    for ems, files in per_ems.items():
        files.sort(key=lambda f: f.period_start)
        later_first = datetime.max.replace(tzinfo=UTC)
        marked = []
        for f in reversed(files):
            marked.append(replace(f, late=f.first_arrival > later_first))
            later_first = min(later_first, f.first_arrival)
        per_ems[ems] = marked[::-1]
    return per_ems


def files_table(files: list[FileState], load_id: str) -> pa.Table:
    """Rows of silver.pm_files.

    Args:
        files: File states.
        load_id: The silver load.

    Returns:
        The rows.
    """
    stamp = pa.timestamp("us", tz="UTC")
    return pa.table(
        {
            "ems": pa.array([f.ems for f in files], pa.string()),
            "file_name": pa.array([f.file_name for f in files], pa.string()),
            "period_start": pa.array([f.period_start for f in files], stamp),
            "period_end": pa.array([f.period_end for f in files], stamp),
            "first_arrival": pa.array([f.first_arrival for f in files], stamp),
            "deliveries": pa.array([f.deliveries for f in files], pa.int32()),
            "versions": pa.array([f.versions for f in files], pa.int32()),
            "latest_hash": pa.array([f.latest_hash for f in files], pa.string()),
            "late": pa.array([f.late for f in files], pa.bool_()),
            "load_id": pa.array([load_id] * len(files), pa.string()),
        }
    )


def base_release(ems: Ems) -> Dialect:
    """The release an EMS's files carry when the format declares none (OMeS).

    Args:
        ems: The EMS.

    Returns:
        The EMS's configured dictionary release (ASSUMPTION: the collector
        knows each EMS's software release from its configuration).
    """
    return ems.dialect


class SilverBuild:
    """Builds silver incrementally from bronze."""

    def __init__(self, con: duckdb.DuckDBPyConnection, load_id: str, grace: timedelta) -> None:
        """Prepare a build.

        Args:
            con: DuckDB with bronze and silver in catalog "lk".
            load_id: Id of this silver run.
            grace: Time after a UTC day's end before it is built.
        """
        self.con = con
        self.load_id = load_id
        self.grace = grace
        self.arrivals = con.execute(
            f"SELECT ems, file_name, file_hash, arrival_time, loaded "
            f"FROM {BRONZE}.file_arrivals WHERE kind = 'PM'"
        ).fetchall()
        self.stats: dict[str, Any] = {"partitions": 0, "catch_up_files": 0, "silver_rows": 0}
        con.execute(f"CREATE OR REPLACE TEMP TABLE silver_map AS SELECT * FROM {MAP}")

    def run(self, until: datetime | None) -> dict[str, Any]:
        """Build every partition whose cutoff has passed, then merge late files.

        Args:
            until: Aware time to build up to, or None for everything bronze
                holds (the latest arrival).

        Returns:
            Counts of the run.

        Raises:
            RuntimeError: If bronze holds no PM files.
        """
        if not self.arrivals:
            raise RuntimeError("bronze holds no PM files")
        latest = max(a[3] for a in self.arrivals)
        now = latest if until is None else min(until, latest)
        known = file_states(self.arrivals, now)
        span_lo = min(f.period_start for files in known.values() for f in files)
        span_hi = max(f.period_end for files in known.values() for f in files)
        self.span = (span_lo, span_hi)
        built = {
            (window.astimezone(UTC).date(), ems)
            for ems, window in self.con.execute(
                f"SELECT ems, window_start FROM {LOADS} WHERE kind = 'partition'"
            ).fetchall()
        }
        row = self.con.execute(f"SELECT max(cutoff) FROM {LOADS}").fetchone()
        watermark: datetime | None = None if row is None else row[0]
        day = span_lo.date()
        while day <= (span_hi - timedelta(microseconds=1)).date():
            lo = datetime(day.year, day.month, day.day, tzinfo=UTC)
            hi = lo + timedelta(days=1)
            cutoff = hi + self.grace
            if cutoff > now:
                break
            todo = [e for e in EMS_LIST if (day, e.ems_id) not in built]
            if todo:
                self.catch_up(watermark, cutoff, built)
                for ems in todo:
                    self.build_partition(ems, lo, hi, cutoff)
                    built.add((day, ems.ems_id))
                watermark = cutoff
            day += timedelta(days=1)
        if watermark is None or now > watermark:
            self.catch_up(watermark, now, built)
        return self.stats

    def stage(self, ems: Ems, lo: datetime, hi: datetime, cutoff: datetime) -> int:
        """Stage silver rows of one EMS for periods starting in [lo, hi).

        Reads the bronze rows of files that arrived by the cutoff, keeps the
        latest version of each file, maps and converts, flags conflicts,
        derives the 3GPP measurements and runs the load checks.

        Args:
            ems: The EMS.
            lo: Aware window start.
            hi: Aware window end.
            cutoff: Aware cutoff.

        Returns:
            Bronze rows read.

        Raises:
            RuntimeError: If a load check fails.
        """
        con = self.con
        files = file_states(self.arrivals, cutoff).get(ems.ems_id, [])
        con.register("file_state", files_table(files, self.load_id))
        con.execute(
            f"""CREATE OR REPLACE TEMP TABLE part AS
            SELECT period_start, period_end, managed_element, object_dn, meas_group, counter,
                value, suspect, coalesce(dictionary_release, '{base_release(ems).release}')
                AS release, file_name, file_hash, arrival_time
            FROM {BRONZE}.pm_values
            WHERE ems = '{ems.ems_id}' AND period_start >= {ts(lo)} AND period_start < {ts(hi)}
                AND arrival_time <= {ts(cutoff)}"""
        )
        bronze_rows = scalar(con, "SELECT count(*) FROM part")
        unknown = con.execute(
            "SELECT DISTINCT file_name FROM part ANTI JOIN file_state USING (file_name)"
        ).fetchall()
        if unknown:
            raise RuntimeError(f"{ems.ems_id}: bronze rows of unrecorded files {unknown[:3]}")
        con.execute(
            """CREATE OR REPLACE TEMP TABLE conflicts AS
            SELECT DISTINCT n.file_name, n.object_dn, n.counter, n.period_start, n.period_end
            FROM part n
            JOIN file_state f ON f.file_name = n.file_name AND n.file_hash = f.latest_hash
                AND f.versions > 1
            JOIN part o ON o.file_name = n.file_name AND o.file_hash <> n.file_hash
                AND o.object_dn = n.object_dn AND o.counter = n.counter
                AND o.period_start = n.period_start AND o.period_end = n.period_end
            WHERE o.value IS DISTINCT FROM n.value"""
        )
        con.execute(
            f"""CREATE OR REPLACE TEMP TABLE staged AS
            SELECT p.period_start, p.period_end,
                CAST(date_diff('minute', p.period_start, p.period_end) AS INTEGER)
                    AS granularity_min,
                '{ems.ems_id}' AS ems, '{ems.dialect.vendor}' AS vendor,
                p.managed_element, p.object_dn, m.measurement, m.bin,
                p.value * m.factor AS value, false AS derived, p.suspect, f.late,
                f.versions, c.file_name IS NOT NULL AS conflict,
                p.release AS dictionary_release, p.counter AS vendor_counter,
                p.file_name, p.file_hash, p.arrival_time
            FROM part p
            JOIN file_state f ON f.file_name = p.file_name AND f.latest_hash = p.file_hash
            LEFT JOIN silver_map m ON m.release = p.release AND m.meas_group = p.meas_group
                AND m.vendor_counter = p.counter
            LEFT JOIN conflicts c ON c.file_name = p.file_name AND c.object_dn = p.object_dn
                AND c.counter = p.counter AND c.period_start = p.period_start
                AND c.period_end = p.period_end"""
        )
        con.unregister("file_state")
        unmapped = con.execute(
            "SELECT DISTINCT dictionary_release, vendor_counter FROM staged "
            "WHERE measurement IS NULL"
        ).fetchall()
        if unmapped:
            raise RuntimeError(f"{ems.ems_id}: counters with no silver mapping: {unmapped}")
        self.derive()
        self.check(ems)
        return int(bronze_rows)

    def derive(self) -> None:
        """Add the 3GPP measurements derived from vendor-style quantities."""
        for measurement, used, total, formula in (
            (
                PRB_PERCENT,
                PRB_USED,
                PRB_AVAIL,
                "round(100 * {used} / {total})",
            ),
            (
                UNAVAILABLE_TIME,
                SAMPLES_AVAILABLE,
                SAMPLES_TOTAL,
                "60 * any_value(granularity_min) * (1 - {used} / {total})",
            ),
        ):
            used_value = f"max(value) FILTER (WHERE measurement = '{used}')"
            total_value = f"max(value) FILTER (WHERE measurement = '{total}')"
            value = formula.format(used=used_value, total=total_value)
            self.con.execute(
                f"""INSERT INTO staged
                SELECT period_start, period_end, granularity_min, any_value(ems),
                    any_value(vendor), any_value(managed_element), object_dn,
                    '{measurement}', {NO_BIN}, {value}, true, bool_or(suspect), bool_or(late),
                    max(versions), bool_or(conflict), any_value(dictionary_release),
                    string_agg(DISTINCT vendor_counter, ' / ' ORDER BY vendor_counter),
                    any_value(file_name), any_value(file_hash), max(arrival_time)
                FROM staged WHERE measurement IN ('{used}', '{total}')
                GROUP BY period_start, period_end, granularity_min, object_dn"""
            )

    def check(self, ems: Ems) -> None:
        """Per-load quality checks on the staged rows.

        Args:
            ems: The EMS.

        Raises:
            RuntimeError: If a check fails.
        """
        con = self.con
        duplicates = scalar(
            con,
            "SELECT count(*) - count(DISTINCT (object_dn, measurement, bin, period_start, "
            "granularity_min)) FROM staged",
        )
        if duplicates:
            raise RuntimeError(f"{ems.ems_id}: {duplicates} duplicate silver keys")
        negative = con.execute(
            "SELECT DISTINCT measurement FROM staged "
            "WHERE value < 0 AND measurement NOT LIKE '%dBm%'"
        ).fetchall()
        if negative:
            raise RuntimeError(f"{ems.ems_id}: negative values in {negative}")
        percent = scalar(
            con,
            f"SELECT count(*) FROM staged WHERE measurement = '{PRB_PERCENT}' "
            "AND (value < 0 OR value > 100)",
        )
        if percent:
            raise RuntimeError(f"{ems.ems_id}: {percent} PRB percentages outside 0 to 100")
        granularity = con.execute(
            "SELECT DISTINCT granularity_min FROM staged WHERE granularity_min NOT IN (15, 60)"
        ).fetchall()
        if granularity:
            raise RuntimeError(f"{ems.ems_id}: unexpected granularities {granularity}")

    def detect_gaps(self, ems: Ems, lo: datetime, hi: datetime, expected_sql: str) -> int:
        """Record expected elements with no values in a period (never zero-filled).

        Args:
            ems: The EMS.
            lo: Aware window start.
            hi: Aware window end.
            expected_sql: Query of (managed_element, granularity_min) expected
                in the window's day.

        Returns:
            Gaps recorded.
        """
        span_lo, span_hi = self.span
        start, end = max(lo, span_lo), min(hi, span_hi)
        if start >= end:
            return 0
        self.con.execute(
            f"""CREATE OR REPLACE TEMP TABLE new_gaps AS
            WITH expected AS ({expected_sql}),
            grid AS (
                SELECT granularity_min, unnest(generate_series({ts(start)},
                    {ts(end)} - to_minutes(granularity_min), to_minutes(granularity_min)))
                    AS period_start
                FROM (VALUES (15::INTEGER), (60::INTEGER)) g(granularity_min)
            ),
            present AS (
                SELECT DISTINCT managed_element, granularity_min, period_start FROM staged
            )
            SELECT '{ems.ems_id}' AS ems, e.managed_element, g.period_start,
                g.period_start + to_minutes(g.granularity_min) AS period_end,
                g.granularity_min, '{self.load_id}' AS load_id
            FROM expected e
            JOIN grid g USING (granularity_min)
            ANTI JOIN present p ON p.managed_element = e.managed_element
                AND p.granularity_min = g.granularity_min AND p.period_start = g.period_start
            WHERE g.granularity_min = 15 OR date_part('minute', g.period_start) = 0"""
        )
        write(self.con, f"INSERT INTO {GAPS} SELECT * FROM new_gaps")
        return int(scalar(self.con, "SELECT count(*) FROM new_gaps"))

    def build_partition(self, ems: Ems, lo: datetime, hi: datetime, cutoff: datetime) -> None:
        """Build one partition (a UTC day of one EMS) from bronze.

        Args:
            ems: The EMS.
            lo: Aware day start.
            hi: Aware day end.
            cutoff: Aware cutoff.
        """
        started = time.perf_counter()
        bronze_rows = self.stage(ems, lo, hi, cutoff)
        write(self.con, f"INSERT INTO {MEASUREMENTS} SELECT *, '{self.load_id}' FROM staged")
        files = [
            f
            for f in file_states(self.arrivals, cutoff).get(ems.ems_id, [])
            if lo <= f.period_start < hi
        ]
        self.write_files(files, None)
        gaps = self.detect_gaps(
            ems, lo, hi, "SELECT DISTINCT managed_element, granularity_min FROM staged"
        )
        self.record("partition", ems, lo, hi, cutoff, len(files), bronze_rows, gaps, started)
        self.stats["partitions"] += 1

    def catch_up(
        self, after: datetime | None, cutoff: datetime, built: set[tuple[date, str]]
    ) -> None:
        """Merge files that arrived in (after, cutoff] into partitions already built.

        Args:
            after: Aware lower bound (exclusive), or None for no bound.
            cutoff: Aware upper bound (inclusive).
            built: Partitions built, (UTC day, EMS).
        """
        states = file_states(self.arrivals, cutoff)
        arrived = {
            (ems, name)
            for ems, name, _, arrival, _ in self.arrivals
            if (after is None or arrival > after) and arrival <= cutoff
        }
        for ems in EMS_LIST:
            for f in states.get(ems.ems_id, []):
                if (ems.ems_id, f.file_name) not in arrived:
                    continue
                if (f.period_start.date(), ems.ems_id) not in built:
                    continue
                self.merge_file(ems, f, cutoff)

    def merge_file(self, ems: Ems, f: FileState, cutoff: datetime) -> None:
        """Merge one file that arrived after its partition was built.

        The window is the file's period, widened to its hour when the file
        closes an hour (its 60-minute block starts there).

        Args:
            ems: The EMS.
            f: The file.
            cutoff: Aware cutoff.
        """
        started = time.perf_counter()
        hi = f.period_end
        lo = hi - timedelta(hours=1) if hi.minute == 0 else f.period_start
        bronze_rows = self.stage(ems, lo, hi, cutoff)
        columns = column_names(self.con, MEASUREMENTS)
        keys = ("ems", "object_dn", "measurement", "bin", "period_start", "granularity_min")
        on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        updates = ", ".join(f"{c} = s.{c}" for c in columns if c not in keys)
        write(
            self.con,
            f"""MERGE INTO {MEASUREMENTS} t
            USING (SELECT *, '{self.load_id}' AS load_id FROM staged) s
            ON {on} AND t.ems = '{ems.ems_id}' AND t.period_start >= {ts(lo)}
                AND t.period_start < {ts(hi)}
            WHEN MATCHED THEN UPDATE SET {updates}
            WHEN NOT MATCHED THEN INSERT ({", ".join(columns)})
                VALUES ({", ".join(f"s.{c}" for c in columns)})""",
        )
        self.write_files([f], (lo, hi))
        write(
            self.con,
            f"DELETE FROM {GAPS} WHERE ems = '{ems.ems_id}' AND period_start >= {ts(lo)} "
            f"AND period_start < {ts(hi)}",
        )
        day = datetime(lo.year, lo.month, lo.day, tzinfo=UTC)
        gaps = self.detect_gaps(
            ems,
            lo,
            hi,
            f"SELECT DISTINCT managed_element, granularity_min FROM {MEASUREMENTS} "
            f"WHERE ems = '{ems.ems_id}' AND period_start >= {ts(day)} "
            f"AND period_start < {ts(day + timedelta(days=1))}",
        )
        self.record("catch-up", ems, lo, hi, cutoff, 1, bronze_rows, gaps, started)
        self.stats["catch_up_files"] += 1

    def write_files(self, files: list[FileState], merge: tuple[datetime, datetime] | None) -> None:
        """Write file states to silver.pm_files.

        Args:
            files: The files.
            merge: None to insert (a new partition), else the window being
                merged (the files may already be there).
        """
        if not files:
            return
        self.con.register("new_files", files_table(files, self.load_id))
        try:
            if merge is None:
                write(self.con, f"INSERT INTO {FILES} SELECT * FROM new_files")
            else:
                columns = column_names(self.con, FILES)
                updates = ", ".join(
                    f"{c} = s.{c}" for c in columns if c not in ("ems", "file_name")
                )
                write(
                    self.con,
                    f"""MERGE INTO {FILES} t USING new_files s
                    ON t.ems = s.ems AND t.file_name = s.file_name
                    WHEN MATCHED THEN UPDATE SET {updates}
                    WHEN NOT MATCHED THEN INSERT ({", ".join(columns)})
                        VALUES ({", ".join(f"s.{c}" for c in columns)})""",
                )
        finally:
            self.con.unregister("new_files")

    def record(
        self,
        kind: str,
        ems: Ems,
        lo: datetime,
        hi: datetime,
        cutoff: datetime,
        files: int,
        bronze_rows: int,
        gaps: int,
        started: float,
    ) -> None:
        """Record one load and its measured quality in silver.loads.

        Args:
            kind: "partition" or "catch-up".
            ems: The EMS.
            lo: Aware window start.
            hi: Aware window end.
            cutoff: Aware cutoff.
            files: Files written.
            bronze_rows: Bronze rows read.
            gaps: Gaps recorded.
            started: perf_counter() at the start of the load.
        """
        row = self.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE derived), count(*) FILTER (WHERE suspect), "
            "count(*) FILTER (WHERE conflict), count(*) FILTER (WHERE late) FROM staged"
        ).fetchone()
        if row is None:
            raise RuntimeError("no counts from staged")
        silver_rows, derived, suspect, conflict, late = (int(v) for v in row)
        self.stats["silver_rows"] += silver_rows if kind == "partition" else 0
        stamp = pa.timestamp("us", tz="UTC")
        load = pa.table(
            {
                "load_id": pa.array([self.load_id], pa.string()),
                "kind": pa.array([kind], pa.string()),
                "ems": pa.array([ems.ems_id], pa.string()),
                "window_start": pa.array([lo], stamp),
                "window_end": pa.array([hi], stamp),
                "cutoff": pa.array([cutoff], stamp),
                **{
                    name: pa.array([value], pa.int64())
                    for name, value in (
                        ("files", files),
                        ("bronze_rows", bronze_rows),
                        ("silver_rows", silver_rows),
                        ("derived_rows", derived),
                        ("suspect_rows", suspect),
                        ("conflict_rows", conflict),
                        ("late_rows", late),
                        ("gaps", gaps),
                    )
                },
                "seconds": pa.array([time.perf_counter() - started], pa.float64()),
            }
        )
        self.con.register("new_load", load)
        try:
            write(self.con, f"INSERT INTO {LOADS} SELECT * FROM new_load")
        finally:
            self.con.unregister("new_load")


def column_names(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Column names of a table, in order.

    Args:
        con: DuckDB.
        table: Fully qualified table.

    Returns:
        The names.
    """
    return [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]


def scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    """One value from a query.

    Args:
        con: DuckDB.
        sql: The query.

    Returns:
        The first column of the first row.

    Raises:
        RuntimeError: If the query returns no row.
    """
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"no row from {sql}")
    return row[0]
