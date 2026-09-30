"""The KPI catalog (rules L3, E4): every gold KPI with its formula in
counters, its source, unit and granularities, and where operators differ.

The formulas run in the dbt project (transform/macros/kpis.sql); this
catalog states them. Clauses are from 3GPP TS 32.450 V19.0.0 (E-UTRAN
KPIs) and TS 32.410 V19.0.0 (GERAN and UTRAN KPIs); counter names from
TS 32.425 (LTE) and TS 52.402 (GSM). Every KPI is a ratio of sums over the
window's reported periods.

Breach thresholds drive the weekly worst-cell ranking (rule L5); they are
START values, set from the daily values of the synthetic demo network so
that about 1 % of cells are persistent worst cells in a week.
"""

from dataclasses import dataclass
from datetime import date

import pyarrow as pa

GRANULARITIES = ("15m", "hour", "day", "week")
HOURLY_ONLY = ("hour", "day", "week")
BOTH = ("huawei", "nokia")


@dataclass(frozen=True)
class Kpi:
    """One KPI at one formula version.

    Attributes:
        kpi_id: Stable id.
        formula_version: Version of the formula (rule D6).
        name: Name.
        technology: "LTE" or "GSM".
        formula: Formula in counters, as computed.
        source: Standard (document, release, clause) or "operator-defined".
        unit: Unit.
        granularities: Granularities computed.
        vendors: Vendor dictionaries that carry the counters it needs.
        better: "higher" or "lower".
        breach_threshold: Daily breach threshold for the worst-cell
            ranking (START), or None when the KPI is not ranked.
        effective_from: First WIB day the version is the operator's
            current one; None for a first version.
        operators_differ: Where operators commonly define it differently.
    """

    kpi_id: str
    formula_version: int
    name: str
    technology: str
    formula: str
    source: str
    unit: str
    granularities: tuple[str, ...]
    vendors: tuple[str, ...]
    better: str
    breach_threshold: float | None
    effective_from: date | None
    operators_differ: str


TS_32450 = "3GPP TS 32.450 V19.0.0"
TS_32410 = "3GPP TS 32.410 V19.0.0"
OPERATOR = "operator-defined"
VENDOR_STYLE = "operator-defined, vendor-style definition"

KPIS = (
    Kpi(
        "LTE_ERAB_ACC",
        1,
        "E-RAB accessibility",
        "LTE",
        "100 * (RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum) * "
        "(S1SIG.ConnEstabSucc / S1SIG.ConnEstabAtt) * "
        "(ERAB.EstabInitSuccNbr.sum / ERAB.EstabInitAttNbr.sum)",
        f"{TS_32450} clause 6.1.1",
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        92.0,
        None,
        "Which RRC establishment causes count (mobile-originated only, or all "
        "including emergency and signalling), and whether the S1 term is included.",
    ),
    Kpi(
        "LTE_ERAB_RET",
        1,
        "E-RAB retainability (R2, UE level)",
        "LTE",
        "3600 * ERAB.RelActNbr.sum / ERAB.SessionTimeUE",
        f"{TS_32450} clause 6.2.1",
        "releases per session hour",
        GRANULARITIES,
        ("nokia",),
        "lower",
        None,
        None,
        "Per QCI (R1) or per UE (R2); many operators report a drop rate per "
        "established E-RAB instead (LTE_ERAB_DROP). The Huawei-style dictionary "
        "carries no session-time counter, so it is computed for Nokia-style cells only.",
    ),
    Kpi(
        "LTE_IP_THP_DL",
        1,
        "E-UTRAN IP throughput, downlink",
        "LTE",
        "1000 * DRB.IPVolDl.sum [kbit] / DRB.IPTimeDl.sum [ms]",
        f"{TS_32450} clause 6.3.1",
        "kbit/s",
        GRANULARITIES,
        BOTH,
        "higher",
        None,
        None,
        "Whether the last TTI emptying the buffer is excluded (TS 32.450 excludes "
        "it); many operators also report cell throughput over the whole period.",
    ),
    Kpi(
        "LTE_AVAIL",
        1,
        "E-UTRAN cell availability",
        "LTE",
        "100 * (900 * reported periods - RRU.CellUnavailableTime.sum) / (900 * reported periods)",
        f"{TS_32450} clause 6.4.1",
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        99.0,
        None,
        "Whether planned maintenance and energy-saving shutdowns count as "
        "unavailable. Nokia-style availability is sampled every 10 s (ASSUMPTION), "
        "so its unavailable time has 10 s resolution.",
    ),
    Kpi(
        "LTE_MOB_HOSR",
        1,
        "E-UTRAN mobility (handover success)",
        "LTE",
        "100 * sum over neighbour relations of HO.OutSuccTarget.sum / sum of HO.OutAttTarget.sum",
        f"{TS_32450} clause 6.5.1, execution phase: its HO.ExeSucc / HO.ExeAtt mapped "
        "to the TS 32.425 per-relation HO.OutSuccTarget / HO.OutAttTarget",
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        97.0,
        None,
        "Whether the preparation phase is included (TS 32.450 includes it; the "
        "model has no preparation counters) and whether inter-RAT handovers count.",
    ),
    Kpi(
        "LTE_RRC_SSR",
        1,
        "RRC setup success rate",
        "LTE",
        "100 * RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum",
        OPERATOR,
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        95.0,
        None,
        "Establishment causes counted; whether re-attempts within a few seconds "
        "count once; whether the S1 signalling connection must also succeed (v2).",
    ),
    Kpi(
        "LTE_ERAB_DROP",
        1,
        "E-RAB drop rate",
        "LTE",
        "100 * ERAB.RelActNbr.sum / ERAB.EstabInitSuccNbr.sum",
        OPERATOR,
        "%",
        GRANULARITIES,
        BOTH,
        "lower",
        2.5,
        None,
        "Which release causes count as drops (ERAB.RelActNbr counts abnormal "
        "releases with data in the buffer) and the denominator (established "
        "E-RABs, incoming handovers included or not).",
    ),
    Kpi(
        "LTE_PRB_UTIL",
        1,
        "DL PRB utilization",
        "LTE",
        "sum of RRU.PrbTotDl [%] / reported periods",
        OPERATOR,
        "%",
        GRANULARITIES,
        BOTH,
        "lower",
        80.0,
        None,
        "Mean over all periods or over the busy hour only; DL only or DL and UL. Gold's "
        "values are per cell; over several cells (an area or the network) PRB utilization "
        "is weighted by each cell's downlink resource blocks: sum(RRU.PrbTotDl * N_RB) / "
        "sum(N_RB), N_RB from the cell's bandwidth (TS 36.101 Table 5.6-1, gold.cells).",
    ),
    Kpi(
        "LTE_CQI_MEAN",
        1,
        "Mean downlink wideband CQI",
        "LTE",
        "sum over bins i of i * CARR.WBCQIDist.Bin[i] / sum of CARR.WBCQIDist.Bin[i] "
        "(CQI index 0 to 15, TS 36.213 Table 7.2.3-1)",
        OPERATOR,
        "CQI index",
        HOURLY_ONLY,
        BOTH,
        "higher",
        None,
        None,
        "Mean index or share of samples at CQI 7 and above. Reported hourly "
        "(rules P1, P4), so it has no 15-minute value.",
    ),
    Kpi(
        "GSM_SAS",
        1,
        "GERAN service access success rate, CS",
        "GSM",
        "100 * (succTCHSeizures / attTCHSeizures) * "
        "(succImmediateAssingProcs / attImmediateAssingProcs)",
        f"{TS_32410} clause 7.4",
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        96.5,
        None,
        "TS 32.410 excludes SDCCH set-up repetitions; vendor counters often do "
        "not. attTCHSeizures here is the vendor TCH request count minus the "
        "requests that met all TCHs busy.",
    ),
    Kpi(
        "GSM_ABN_REL",
        1,
        "GERAN service abnormal release rate",
        "GSM",
        "100 * (nbrOfLostRadioLinksTCH + unsuccHDOsWithReconnection + "
        "unsuccHDOsWithLossOfConnection) / (succTCHSeizures + "
        "succIncomingInternalInterCellHDOs)",
        f"{TS_32410} clause 8.2, without the intra-cell handover terms (not modelled)",
        "%",
        GRANULARITIES,
        ("huawei",),
        "lower",
        None,
        None,
        "Operators usually call a TCH drop rate their own formula; the "
        "Nokia-style dictionary carries no unsuccessful-handover counter, so it "
        "is computed for Huawei-style cells only.",
    ),
    Kpi(
        "GSM_HOSR",
        1,
        "Handover success rate (cell)",
        "GSM",
        "100 * succOutgoingInternalInterCellHDOs / attOutgoingInternalInterCellHDOs",
        f"{TS_32410} clause 9.5",
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        94.5,
        None,
        "Internal only or including external (inter-BSC) handovers.",
    ),
    Kpi(
        "GSM_CSSR",
        1,
        "Call setup success rate",
        "GSM",
        "100 * (1 - attSDCCHSeizuresMeetingSDCCHBlockedState / attImmediateAssingProcs) "
        "* succTCHSeizures / (attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState)",
        VENDOR_STYLE,
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        96.0,
        None,
        "The most varied GSM KPI: vendors and operators multiply different "
        "SDCCH, TCH assignment and drop terms.",
    ),
    Kpi(
        "GSM_TCH_BLOCK",
        1,
        "TCH blocking",
        "GSM",
        "100 * attTCHSeizuresMeetingTCHBlockedState / "
        "(attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState)",
        VENDOR_STYLE,
        "%",
        GRANULARITIES,
        BOTH,
        "lower",
        3.0,
        None,
        "Denominator with or without the blocked requests; handover requests counted or not.",
    ),
    Kpi(
        "GSM_SDCCH_BLOCK",
        1,
        "SDCCH blocking",
        "GSM",
        "100 * attSDCCHSeizuresMeetingSDCCHBlockedState / attImmediateAssingProcs",
        VENDOR_STYLE,
        "%",
        GRANULARITIES,
        BOTH,
        "lower",
        1.0,
        None,
        "Denominator: immediate assignment attempts or SDCCH seizure attempts.",
    ),
    Kpi(
        "GSM_SDCCH_DROP",
        1,
        "SDCCH drop rate",
        "GSM",
        "100 * nbrOfLostRadioLinksSDCCH / succImmediateAssingProcs",
        VENDOR_STYLE,
        "%",
        GRANULARITIES,
        ("nokia",),
        "lower",
        None,
        None,
        "Which SDCCH releases count as drops. The Huawei-style dictionary "
        "carries no SDCCH lost-radio-link counter, so it is computed for "
        "Nokia-style cells only.",
    ),
)


def revised(kpi_id: str, version: int, effective_from: date) -> Kpi:
    """The catalog entry of a revised formula (rule D6).

    Args:
        kpi_id: The KPI revised.
        version: Its new version.
        effective_from: First WIB day it is the operator's current formula.

    Returns:
        The entry.

    Raises:
        ValueError: If no revision of that KPI and version is defined.
    """
    if (kpi_id, version) != ("LTE_RRC_SSR", 2):
        raise ValueError(f"no revision {kpi_id} v{version} is defined")
    return Kpi(
        "LTE_RRC_SSR",
        2,
        "RRC setup success rate (with S1 signalling)",
        "LTE",
        "100 * (RRC.ConnEstabSucc.sum / RRC.ConnEstabAtt.sum) * "
        "(S1SIG.ConnEstabSucc / S1SIG.ConnEstabAtt)",
        OPERATOR,
        "%",
        GRANULARITIES,
        BOTH,
        "higher",
        95.0,
        effective_from,
        "Revised by the operator: a set-up now counts only when the S1 "
        "signalling connection also succeeds. History is reprocessed; v1 and "
        "v2 are both kept.",
    )


def catalog_table(kpis: tuple[Kpi, ...]) -> pa.Table:
    """The catalog as rows of gold.kpi_catalog.

    Args:
        kpis: Catalog entries.

    Returns:
        The rows.
    """
    return pa.table(
        {
            "kpi_id": pa.array([k.kpi_id for k in kpis], pa.string()),
            "formula_version": pa.array([k.formula_version for k in kpis], pa.int32()),
            "name": pa.array([k.name for k in kpis], pa.string()),
            "technology": pa.array([k.technology for k in kpis], pa.string()),
            "formula": pa.array([k.formula for k in kpis], pa.string()),
            "source": pa.array([k.source for k in kpis], pa.string()),
            "unit": pa.array([k.unit for k in kpis], pa.string()),
            "granularities": pa.array([",".join(k.granularities) for k in kpis], pa.string()),
            "vendors": pa.array([",".join(k.vendors) for k in kpis], pa.string()),
            "better": pa.array([k.better for k in kpis], pa.string()),
            "breach_threshold": pa.array([k.breach_threshold for k in kpis], pa.float64()),
            "effective_from": pa.array([k.effective_from for k in kpis], pa.date32()),
            "operators_differ": pa.array([k.operators_differ for k in kpis], pa.string()),
        }
    )


RRC = ("RRC.ConnEstabAtt.sum", "RRC.ConnEstabSucc.sum")
S1 = ("S1SIG.ConnEstabAtt", "S1SIG.ConnEstabSucc")
TCH_REQUESTS = "attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState"
TCH_BLOCKED = "attTCHSeizuresMeetingTCHBlockedState"
IMMEDIATE = ("attImmediateAssingProcs", "succImmediateAssingProcs")

# The silver measurements each KPI version reads (rule D8 lineage), under
# their silver names (lake.silver): 3GPP names, or labelled sums.
INPUTS: dict[tuple[str, int], tuple[str, ...]] = {
    ("LTE_ERAB_ACC", 1): (*RRC, *S1, "ERAB.EstabInitAttNbr.sum", "ERAB.EstabInitSuccNbr.sum"),
    ("LTE_ERAB_RET", 1): ("ERAB.RelActNbr.sum", "ERAB.SessionTimeUE"),
    ("LTE_IP_THP_DL", 1): ("DRB.IPVolDl.sum", "DRB.IPTimeDl.sum"),
    ("LTE_AVAIL", 1): ("RRU.CellUnavailableTime.sum",),
    ("LTE_MOB_HOSR", 1): ("HO.OutAttTarget.sum", "HO.OutSuccTarget.sum"),
    ("LTE_RRC_SSR", 1): RRC,
    ("LTE_RRC_SSR", 2): (*RRC, *S1),
    ("LTE_ERAB_DROP", 1): ("ERAB.RelActNbr.sum", "ERAB.EstabInitSuccNbr.sum"),
    ("LTE_PRB_UTIL", 1): ("RRU.PrbTotDl",),
    ("LTE_CQI_MEAN", 1): ("CARR.WBCQIDist.Bin",),
    ("GSM_SAS", 1): (TCH_REQUESTS, TCH_BLOCKED, "succTCHSeizures", *IMMEDIATE),
    ("GSM_ABN_REL", 1): (
        "nbrOfLostRadioLinksTCH",
        "unsuccHDOsWithReconnection + unsuccHDOsWithLossOfConnection",
        "succTCHSeizures",
        "succIncomingInternalInterCellHDOs",
    ),
    ("GSM_HOSR", 1): ("attOutgoingInternalInterCellHDOs", "succOutgoingInternalInterCellHDOs"),
    ("GSM_CSSR", 1): (
        "attSDCCHSeizuresMeetingSDCCHBlockedState",
        "attImmediateAssingProcs",
        "succTCHSeizures",
        TCH_REQUESTS,
    ),
    ("GSM_TCH_BLOCK", 1): (TCH_BLOCKED, TCH_REQUESTS),
    ("GSM_SDCCH_BLOCK", 1): ("attSDCCHSeizuresMeetingSDCCHBlockedState", "attImmediateAssingProcs"),
    ("GSM_SDCCH_DROP", 1): ("nbrOfLostRadioLinksSDCCH", "succImmediateAssingProcs"),
}
