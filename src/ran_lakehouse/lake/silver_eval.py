"""What silver did with each planted delivery anomaly (rules D1 to D5, A3).

Reads the evaluation-only answers and compares them with silver's flags.
For evaluation, tests and reports only; the serving API never calls it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.collect.delivery import DeliveryPlan
from ran_lakehouse.files.ems import WIB, nokia_dn
from ran_lakehouse.lake.bronze import EVALUATION
from ran_lakehouse.lake.silver import FILES, GAPS, MEASUREMENTS, scalar, ts
from ran_lakehouse.model import NetworkModel

PERIOD = timedelta(minutes=15)
KINDS = ("D1", "D2_same", "D2_conflict", "D3", "D4", "D5")


@dataclass(frozen=True)
class Outcome:
    """Planted cases and what silver found, for one kind.

    Attributes:
        planted: Planted cases in the span silver covers.
        found: Cases silver flagged.
    """

    planted: frozenset[tuple[Any, ...]]
    found: frozenset[tuple[Any, ...]]

    def counts(self) -> dict[str, int]:
        """Counts for a report (no network element or period is revealed).

        Returns:
            planted, found, and matched (in both).
        """
        return {
            "planted": len(self.planted),
            "found": len(self.found),
            "matched": len(self.planted & self.found),
        }


def vendor_element(model: NetworkModel, ems_id: str, element: str) -> str:
    """A world managed element as its EMS names it in bronze and silver.

    Args:
        model: The network.
        ems_id: The EMS.
        element: World managed element name.

    Returns:
        "ManagedElement=<element>" for the 3GPP XML EMS, the first two DN
        levels for the Nokia-style EMS.

    Raises:
        ValueError: If the EMS is unknown or the element has no cell.
    """
    ems = next((e for e in EMS_LIST if e.ems_id == ems_id), None)
    if ems is None:
        raise ValueError(f"unknown EMS {ems_id}")
    if ems.file_format == "3gpp-xml":
        return f"ManagedElement={element}"
    cell = next((i for i, c in enumerate(model.world.cells) if c.managed_element == element), None)
    if cell is None:
        raise ValueError(f"no cell under {element}")
    return "/".join(nokia_dn(model, cell).split("/")[:2])


def rows(con: duckdb.DuckDBPyConnection, sql: str) -> frozenset[tuple[Any, ...]]:
    """A query's rows as a set.

    Args:
        con: DuckDB.
        sql: The query.

    Returns:
        The rows.
    """
    return frozenset(tuple(r) for r in con.execute(sql).fetchall())


def evaluate(
    con: duckdb.DuckDBPyConnection, model: NetworkModel, plan: DeliveryPlan
) -> dict[str, Outcome]:
    """Compare each planted anomaly kind with silver.

    Only cases silver can see are in scope: periods inside the span of
    delivered files, excluding its first and last period (no file before or
    after them to reveal a gap or a late arrival).

    Args:
        con: DuckDB with evaluation and silver in catalog "lk".
        model: The network the run was simulated on.
        plan: The run's delivery plan (for the D5 upgrade time).

    Returns:
        Kind to outcome.
    """
    span = con.execute(f"SELECT min(period_start), max(period_end) FROM {FILES}").fetchone()
    if span is None or span[0] is None:
        raise RuntimeError("silver.pm_files is empty")
    lo: datetime = span[0] + PERIOD
    hi: datetime = span[1] - PERIOD
    planted = con.execute(
        f"SELECT kind, ems, period_start, managed_element FROM {EVALUATION}.delivery_anomalies"
    ).fetchall()

    def planted_set(kind: str, with_element: bool) -> frozenset[tuple[Any, ...]]:
        return frozenset(
            (ems, start, vendor_element(model, ems, element)) if with_element else (ems, start)
            for k, ems, start, element in planted
            if k == kind and lo <= start < hi
        )

    in_span = f"f.period_start >= {ts(lo)} AND f.period_start < {ts(hi)}"
    by_file = f"FROM {MEASUREMENTS} m JOIN {FILES} f USING (ems, file_name) WHERE {in_span}"
    out = {
        "D1": Outcome(
            planted_set("D1", False),
            rows(con, f"SELECT ems, period_start FROM {FILES} f WHERE late AND {in_span}"),
        ),
        "D2_same": Outcome(
            planted_set("D2_same", False),
            rows(
                con,
                f"SELECT ems, period_start FROM {FILES} f "
                f"WHERE deliveries > versions AND {in_span}",
            ),
        ),
        "D2_conflict": Outcome(
            planted_set("D2_conflict", True),
            rows(
                con,
                f"SELECT DISTINCT m.ems, f.period_start, m.managed_element {by_file} "
                "AND m.conflict",
            ),
        ),
        "D3": Outcome(
            planted_set("D3", False),
            rows(
                con,
                f"""WITH expected AS (
                    SELECT ems, CAST(period_start AS DATE) AS day,
                        count(DISTINCT managed_element) AS elements
                    FROM {MEASUREMENTS} WHERE granularity_min = 15 GROUP BY ALL
                ), missing AS (
                    SELECT ems, period_start, count(*) AS elements FROM {GAPS}
                    WHERE granularity_min = 15 AND period_start >= {ts(lo)}
                        AND period_start < {ts(hi)}
                    GROUP BY ALL
                )
                SELECT m.ems, m.period_start FROM missing m
                JOIN expected e ON e.ems = m.ems AND e.day = CAST(m.period_start AS DATE)
                WHERE m.elements = e.elements""",
            ),
        ),
        "D4": Outcome(
            planted_set("D4", True),
            rows(
                con,
                f"SELECT DISTINCT m.ems, f.period_start, m.managed_element {by_file} AND m.suspect",
            ),
        ),
    }
    out["D5"] = d5(con, model, plan, lo, hi, planted)
    return out


def d5(
    con: duckdb.DuckDBPyConnection,
    model: NetworkModel,
    plan: DeliveryPlan,
    lo: datetime,
    hi: datetime,
    planted: list[tuple[Any, ...]],
) -> Outcome:
    """Elements whose throughput series stays continuous across the rename.

    Found: elements with rows under two dictionary releases whose
    DRB.IPVolDl.sum is present in every 15-minute period of the day before
    and after the upgrade in which the element reported anything.

    Args:
        con: DuckDB with silver.
        model: The network.
        plan: The delivery plan.
        lo: Aware start of the span in scope.
        hi: Aware end of the span in scope.
        planted: Planted anomalies (kind, ems, period_start, element).

    Returns:
        The outcome.
    """
    upgrade = plan.upgrade_time.replace(tzinfo=WIB).astimezone(UTC)
    if not lo <= upgrade < hi:
        return Outcome(frozenset(), frozenset())
    expected = frozenset(
        (ems, vendor_element(model, ems, element))
        for kind, ems, _, element in planted
        if kind == "D5"
    )
    window = f"period_start >= {ts(upgrade - timedelta(days=1))} " + (
        f"AND period_start < {ts(upgrade + timedelta(days=1))} AND granularity_min = 15"
    )
    found = rows(
        con,
        f"""SELECT ems, managed_element FROM {MEASUREMENTS} WHERE {window}
        GROUP BY ems, managed_element
        HAVING count(DISTINCT dictionary_release) = 2
            AND count(DISTINCT period_start)
                = count(DISTINCT period_start) FILTER (WHERE measurement = 'DRB.IPVolDl.sum')""",
    )
    if scalar(con, f"SELECT count(*) FROM {MEASUREMENTS} WHERE {window}") == 0:
        raise RuntimeError("silver has no rows around the upgrade")
    return Outcome(expected, found)
