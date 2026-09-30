"""Gold against the model (rule L3): every daily KPI recomputed from the
simulator's own counters, with the same faults, must agree with gold.

The reference applies the catalog's formulas with numpy to the counters
the files were written from, per cell and WIB day, as ratios of sums over
the day's reported periods. Cell-days whose values a planted delivery
anomaly changed are left out: a corrected re-export (D2 conflict) or an
interrupted collection (D4) on the cell's network element, or a missing
file (D3) of its EMS. Agreement is within the rounding of the file formats
(values written with 10 significant digits, Huawei-style PRBs to 0.1) and,
for availability, the 10-second sampling of the Nokia-style counter.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import duckdb
import numpy as np

from ran_lakehouse.collect.backfill import EMS_LIST
from ran_lakehouse.collect.delivery import DeliveryPlan
from ran_lakehouse.faults.plant import Fault
from ran_lakehouse.faults.simulate import simulate_with_faults
from ran_lakehouse.model import RUN_START, Day, NetworkModel

PERIODS = 96
REL_TOLERANCE = 1e-6
# Nokia-style availability: 90 samples per 15 minutes, so unavailable time
# is known to 10 s; over a day that is at most 96 * 5 s of 86,400 s.
AVAIL_TOLERANCE_PCT = 100 * 96 * 5 / 86_400


@dataclass(frozen=True)
class Comparison:
    """One KPI's agreement between gold and the reference.

    Attributes:
        compared: Cell-days compared.
        agreeing: Cell-days within tolerance.
        max_abs_diff: Largest absolute difference.
        missing_in_gold: Reference cell-days gold has no value for.
    """

    compared: int
    agreeing: int
    max_abs_diff: float
    missing_in_gold: int


def ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """num / den, NaN where den is 0 (gold gives NULL there).

    Args:
        num: Numerator.
        den: Denominator.

    Returns:
        The ratio.
    """
    out = np.full(np.shape(num), np.nan)
    np.divide(num, den, out=out, where=np.asarray(den) != 0)
    return out


def day_references(day: Day, model: NetworkModel) -> Iterator[tuple[str, int, str, float]]:
    """Reference KPIs of every cell for one WIB day.

    Args:
        day: The day's counters.
        model: The network.

    Yields:
        (kpi_id, formula_version, cell_name, value); NaN values are skipped.
    """
    vendor = model.state.vendor
    names = [c.cell_name for c in model.world.cells]
    v = day.lte.values

    def s(name: str) -> np.ndarray:
        total: np.ndarray = np.nansum(v[name], axis=0)
        return total

    rrc = ratio(s("RRC.ConnEstabSucc.sum"), s("RRC.ConnEstabAtt.sum"))
    s1 = ratio(s("S1SIG.ConnEstabSucc"), s("S1SIG.ConnEstabAtt"))
    erab = ratio(s("ERAB.EstabInitSuccNbr.sum"), s("ERAB.EstabInitAttNbr.sum"))
    unavail = v["RRU.CellUnavailableTime.sum"]
    reported = (~np.isnan(unavail)).sum(axis=0)
    prb = v["RRU.PrbTotDl"]
    bins = v["CARR.WBCQIDist.Bin"]
    index = np.arange(bins.shape[2])
    lte = {
        ("LTE_ERAB_ACC", 1): 100 * rrc * s1 * erab,
        ("LTE_ERAB_RET", 1): 3600 * ratio(s("ERAB.RelActNbr.sum"), s("ERAB.SessionTimeUE")),
        ("LTE_IP_THP_DL", 1): 1000 * ratio(s("DRB.IPVolDl.sum"), s("DRB.IPTimeDl.sum")),
        ("LTE_AVAIL", 1): 100 * ratio(900 * reported - np.nansum(unavail, axis=0), 900 * reported),
        ("LTE_MOB_HOSR", 1): 100 * ratio(s("HO.IntraFreqOutSucc"), s("HO.IntraFreqOutAtt")),
        ("LTE_RRC_SSR", 1): 100 * rrc,
        ("LTE_RRC_SSR", 2): 100 * rrc * s1,
        ("LTE_ERAB_DROP", 1): 100 * ratio(s("ERAB.RelActNbr.sum"), s("ERAB.EstabInitSuccNbr.sum")),
        ("LTE_PRB_UTIL", 1): ratio(np.nansum(prb, axis=0), (~np.isnan(prb)).sum(axis=0)),
        ("LTE_CQI_MEAN", 1): ratio(
            np.nansum(bins * index, axis=(0, 2)), np.nansum(bins, axis=(0, 2))
        ),
    }
    # KPIs whose counters only one vendor's dictionary carries (kpi_catalog).
    lte_only = {"LTE_ERAB_RET": "nokia"}
    for (kpi_id, version), values in lte.items():
        for column, cell in enumerate(day.lte.cells):
            if kpi_id in lte_only and vendor[cell] != lte_only[kpi_id]:
                continue
            if not np.isnan(values[column]):
                yield kpi_id, version, names[int(cell)], float(values[column])
    g = day.gsm.values

    def t(name: str) -> np.ndarray:
        total: np.ndarray = np.nansum(g[name], axis=0)
        return total

    att = t("attTCHSeizures")
    blocked = t("attTCHSeizuresMeetingTCHBlockedState")
    ia = ratio(t("succImmediateAssingProcs"), t("attImmediateAssingProcs"))
    sdcch_blocked = ratio(
        t("attSDCCHSeizuresMeetingSDCCHBlockedState"), t("attImmediateAssingProcs")
    )
    gsm = {
        "GSM_SAS": 100 * ratio(t("succTCHSeizures"), att) * ia,
        "GSM_ABN_REL": 100
        * ratio(
            t("nbrOfLostRadioLinksTCH")
            + t("unsuccHDOsWithReconnection")
            + t("unsuccHDOsWithLossOfConnection"),
            t("succTCHSeizures") + t("succIncomingInternalInterCellHDOs"),
        ),
        "GSM_HOSR": 100
        * ratio(t("succOutgoingInternalInterCellHDOs"), t("attOutgoingInternalInterCellHDOs")),
        "GSM_CSSR": 100 * (1 - sdcch_blocked) * ratio(t("succTCHSeizures"), att + blocked),
        "GSM_TCH_BLOCK": 100 * ratio(blocked, att + blocked),
        "GSM_SDCCH_BLOCK": 100 * sdcch_blocked,
        "GSM_SDCCH_DROP": 100 * ratio(t("nbrOfLostRadioLinksSDCCH"), t("succImmediateAssingProcs")),
    }
    gsm_only = {"GSM_ABN_REL": "huawei", "GSM_SDCCH_DROP": "nokia"}
    for kpi_id, values in gsm.items():
        for column, cell in enumerate(day.gsm.cells):
            if kpi_id in gsm_only and vendor[cell] != gsm_only[kpi_id]:
                continue
            if not np.isnan(values[column]):
                yield kpi_id, 1, names[int(cell)], float(values[column])


def touched(model: NetworkModel, plan: DeliveryPlan) -> set[tuple[str, int]]:
    """(cell name, WIB day index) whose values a planted anomaly changed.

    Args:
        model: The network.
        plan: The delivery plan.

    Returns:
        The cell-days.
    """
    vendor_of = {e.ems_id: e.dialect.vendor for e in EMS_LIST}
    out: set[tuple[str, int]] = set()
    for a in plan.anomalies:
        if a.kind not in ("D2_conflict", "D3", "D4"):
            continue
        day = a.period // PERIODS
        for cell, vendor in zip(model.world.cells, model.state.vendor, strict=True):
            if vendor != vendor_of[a.ems_id]:
                continue
            if a.kind == "D3" or cell.managed_element == a.element:
                out.add((cell.cell_name, day))
    return out


def complete_days(con: duckdb.DuckDBPyConnection) -> set[Any]:
    """WIB days whose two UTC days gold has built.

    A WIB day runs from 17:00 UTC the day before to 17:00 UTC; silver builds
    a UTC day only after its end (lake.silver.GRACE), so the run's last WIB
    day stays partial in gold, its coverage below 1.

    Args:
        con: DuckDB with gold in catalog "lk".

    Returns:
        The WIB dates.
    """
    built = {
        d
        for (d,) in con.execute(
            "SELECT DISTINCT utc_day FROM lk.gold.loads WHERE kind = 'day'"
        ).fetchall()
    }
    return {d + timedelta(days=1) for d in built if d + timedelta(days=1) in built}


def compare(
    con: duckdb.DuckDBPyConnection,
    model: NetworkModel,
    faults: list[Fault],
    plan: DeliveryPlan,
    days: range,
) -> dict[str, Comparison]:
    """Compare gold's daily KPIs with the reference over some WIB days.

    Only WIB days gold has built completely are compared (complete_days).

    Args:
        con: DuckDB with gold in catalog "lk".
        model: The network as built.
        faults: The run's fault schedule.
        plan: The run's delivery plan.
        days: WIB day indices.

    Returns:
        "kpi_id vN" to its comparison.
    """
    first = (RUN_START + timedelta(days=days.start)).date()
    last = (RUN_START + timedelta(days=days.stop - 1)).date()
    gold = {
        (kpi, version, cell, day): value
        for kpi, version, cell, day, value in con.execute(
            "SELECT kpi_id, formula_version, cell_name, day, value FROM ("
            "SELECT * FROM lk.gold.lte_kpi_day UNION ALL SELECT * FROM lk.gold.gsm_kpi_day) "
            f"WHERE day >= DATE '{first}' AND day <= DATE '{last}'"
        ).fetchall()
    }
    skip = touched(model, plan)
    complete = complete_days(con)
    stats: dict[str, dict[str, Any]] = {}
    for day in simulate_with_faults(model, faults, days.start, len(days)):
        date = (RUN_START + timedelta(days=day.index)).date()
        if date not in complete:
            continue
        for kpi_id, version, cell, value in day_references(day, model):
            if (cell, day.index) in skip:
                continue
            key = f"{kpi_id} v{version}"
            entry = stats.setdefault(
                key, {"compared": 0, "agreeing": 0, "max_abs_diff": 0.0, "missing_in_gold": 0}
            )
            got = gold.get((kpi_id, version, cell, date))
            if got is None:
                entry["missing_in_gold"] += 1
                continue
            diff = abs(got - value)
            tolerance = (
                AVAIL_TOLERANCE_PCT
                if kpi_id == "LTE_AVAIL"
                else REL_TOLERANCE * max(1.0, abs(value))
            )
            entry["compared"] += 1
            entry["agreeing"] += int(diff <= tolerance)
            entry["max_abs_diff"] = max(entry["max_abs_diff"], diff)
    return {k: Comparison(**v) for k, v in sorted(stats.items())}
