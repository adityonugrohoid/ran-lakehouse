"""LTE and GSM counters per 15-minute period (rules M3, M4, M5).

Counters use 3GPP measurement names: LTE per TS 32.425 V19.0.0, GSM per TS
52.402 V19.0.0 Annex B. Vendor dialects map from these later (rule P5).
Every transfer function below is ASSUMPTION unless cited; they are simple,
monotonic and documented so the counter relationships can be checked.

Randomness uses common random numbers (rule M6): each cell and day draws
one fixed block of standard normals (rule W1 seeding), and every counter
turns its normals into a count through the same deterministic formula, so
a changed network replays with the same noise.
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.model.cells import TDD_DL_SHARE, CellState
from ran_lakehouse.model.erlang import erlang_b
from ran_lakehouse.model.serving import Serving
from ran_lakehouse.seeds import Purpose, rng

PERIOD_S = 900.0  # granPeriod PT900S (TS 32.435)
N_STREAMS = 9  # standard-normal streams per cell and period

# Load (ASSUMPTION).
CONNECTED_SHARE_AT_PEAK = 0.02  # RRC-connected share of subscribers at the weekday peak
DEMAND_KBPS_PER_CONNECTED = 650.0  # mean DL demand of a connected user
LOAD_NOISE_SIGMA = 0.2  # lognormal sigma of per-period load noise
PRB_OVERHEAD = 0.05  # control and signalling share of PRBs
CARRIED_CEILING = 0.97  # share of capacity the cell can carry
MIN_USER_SHARE = 0.05  # floor of (1 - load) in processor-sharing throughput
MEAN_CONNECTION_S = 20.0  # mean RRC connection duration
CQI_REPORT_PERIOD_S = 0.08  # periodic CQI report interval
# LTE success and drop transfer functions (ASSUMPTION).
RRC_BASE_FAIL = 0.003
S1_BASE_FAIL = 0.001
ERAB_BASE_FAIL = 0.002
CONGESTION_KNEE = 0.9  # load above which setups start failing
# Setup failure from congestion saturates at these shares as load grows past
# the knee: max_fail * (1 - exp(-(load - knee))).
RRC_CONGESTION_MAX_FAIL = 0.08
ERAB_CONGESTION_MAX_FAIL = 0.04
RRC_EDGE_SLOPE = 0.05
ERAB_EDGE_SLOPE = 0.02
DROP_BASE = 0.002
DROP_EDGE_SLOPE = 0.04
# Handover demand towards a target that is not a configured neighbour
# cannot be executed; this share of it ends in radio link failure and an
# abnormal release (ASSUMPTION).
RLF_WITHOUT_NEIGHBOUR = 0.5
HO_BASE_SUCCESS = 0.99
HO_EDGE_SLOPE = 0.02
# Handovers per connected-user second by area class (ASSUMPTION: smaller
# cells, more handovers).
HO_RATE_PER_S = {"urban": 0.004, "suburban": 0.002, "rural": 0.001}
# Uplink interference (rule F1e, ASSUMPTION): setup and drop impairment
# saturates with the uplink noise rise as 1 - exp(-rise / scale).
UL_RISE_SCALE_DB = 6.0
UL_RRC_MAX_FAIL = 0.25
UL_ERAB_MAX_FAIL = 0.10
UL_DROP_MAX = 0.03
# Uplink noise floor per PRB: -174 dBm/Hz over 180 kHz plus a 5 dB base
# station noise figure (TR 36.942 V19.0.0 Table 12.2).
UL_NOISE_FLOOR_DBM = -174.0 + 10.0 * float(np.log10(180_000.0)) + 5.0
# Load adds this much to the measured uplink interference level at full
# load (ASSUMPTION, intra-system uplink interference).
UL_LOAD_RISE_DB = 3.0
# GSM (ASSUMPTION).
ERLANG_PER_USER_AT_PEAK = 0.02
MEAN_HOLDING_S = 90.0
SDCCH_PER_CALL = 2.5  # SDCCH seizures per call (call set-up plus LU and SMS)
SDCCH_HOLDING_S = 3.0
TCH_ASSIGN_FAIL = 0.004
TCH_ASSIGN_EDGE_SLOPE = 0.02
IA_FAIL = 0.005
IA_EDGE_SLOPE = 0.01
TCH_DROP_BASE = 0.006
TCH_DROP_EDGE_SLOPE = 0.02
SDCCH_DROP_BASE = 0.002
SDCCH_DROP_EDGE_SLOPE = 0.01
GSM_HO_PER_CALL = {"urban": 1.2, "suburban": 0.8, "rural": 0.5}
GSM_HO_BASE_SUCCESS = 0.97
GSM_HO_RECONNECT_SHARE = 0.7  # failed handovers that reconnect to the old channel


def gsm_channels(trx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """TCH and SDCCH counts from the transceiver count.

    Eight timeslots per TRX (TS 45.002 V19.0.0 clause 4.3.1); one timeslot
    for BCCH/CCCH and one SDCCH/8 timeslot per two TRX, 8 SDCCH each (TS
    45.002 clause 6.4.1 combinations iv and vii); the rest are TCH/F
    (ASSUMPTION: the allocation per TRX count).

    Args:
        trx: Transceivers per cell.

    Returns:
        Number of TCHs and number of SDCCHs per cell.
    """
    sdcch_slots = np.ceil(trx / 2.0)
    return 8 * trx - 1 - sdcch_slots, 8 * sdcch_slots


def poisson_count(mean: np.ndarray, z: np.ndarray) -> np.ndarray:
    """A Poisson-like count from its mean and a standard normal.

    Args:
        mean: Expected count.
        z: Standard normal draw.

    Returns:
        max(0, round(mean + sqrt(mean) z)).
    """
    result: np.ndarray = np.maximum(0, np.rint(mean + np.sqrt(mean) * z))
    return result


def binomial_count(n: np.ndarray, p: np.ndarray, z: np.ndarray) -> np.ndarray:
    """A binomial-like count from trials, probability and a standard normal.

    Args:
        n: Trials.
        p: Success probability.
        z: Standard normal draw.

    Returns:
        round(n p + sqrt(n p (1 - p)) z), clipped to [0, n].
    """
    p = np.clip(p, 0.0, 1.0)
    result: np.ndarray = np.clip(np.rint(n * p + np.sqrt(n * p * (1.0 - p)) * z), 0, n)
    return result


def noise_block(cells: np.ndarray, day: int, periods: int) -> np.ndarray:
    """Standard normals for a set of cells and one day (rule W1 seeding).

    Args:
        cells: Global cell indices.
        day: Day index from the start of the run.
        periods: Periods per day.

    Returns:
        Array of shape (N_STREAMS, periods, len(cells)).
    """
    block = np.empty((N_STREAMS, periods, cells.size))
    for j, cell in enumerate(cells):
        block[:, :, j] = rng(Purpose.MODEL_NOISE, int(cell) * 100_000 + day).standard_normal(
            (N_STREAMS, periods)
        )
    return block


@dataclass(frozen=True)
class DayCounters:
    """One day of counters for one technology.

    Attributes:
        cells: Global cell indices, the column order of the cell arrays.
        values: Measurement name to array of shape (periods, cells), or
            (periods, cells, bins) for distributions.
        relations: (source, target) global cell indices, the column order of
            the per-relation arrays.
        relation_values: Measurement name to array of shape (periods,
            relations); NaN where the relation is not configured, so not
            reported.
    """

    cells: np.ndarray
    values: dict[str, np.ndarray]
    relations: list[tuple[int, int]]
    relation_values: dict[str, np.ndarray]


@dataclass(frozen=True)
class TechRelations:
    """Handover demand of one technology's cells, by neighbour relation.

    Attributes:
        cells: Global indices of the technology's cells.
        source_col: Column in cells of each demand relation's source.
        target_col: Column in cells of each demand relation's target.
        configured_share: Share of the source's configured demand on each
            relation (0 for relations not configured).
        counter_col: Per-relation counter column of each demand relation,
            -1 when the relation is not a counter column.
        unconfigured: Share of each cell's demand towards targets that are
            not configured neighbours.
        columns: Per-relation counter columns (source, target).
        reported: Whether each counter column is configured, so reported.
    """

    cells: np.ndarray
    source_col: np.ndarray
    target_col: np.ndarray
    configured_share: np.ndarray
    counter_col: np.ndarray
    unconfigured: np.ndarray
    columns: list[tuple[int, int]]
    reported: np.ndarray


@dataclass(frozen=True)
class RelationPlan:
    """Handover demand and neighbour configuration per technology.

    Attributes:
        lte: LTE relations (intra-frequency).
        gsm: GSM relations internal to a BSC (TS 52.402 internal handovers).
    """

    lte: TechRelations
    gsm: TechRelations


def tech_relations(
    state: CellState,
    serving: Serving,
    neighbours: frozenset[tuple[int, int]],
    columns: list[tuple[int, int]],
    technology: str,
) -> TechRelations:
    """Handover demand of one technology split over configured relations.

    Args:
        state: Cell parameters.
        serving: Who each cell serves, with the overlap relations.
        neighbours: Configured neighbour relations.
        columns: Candidate per-relation counter columns (any technology).
        technology: "LTE" or "GSM".

    Returns:
        The technology's relations. For GSM only relations within one BSC
        (same vendor region) count, as TS 52.402 internal handovers.
    """
    n = len(state.cell_names)
    cells = np.flatnonzero(state.technology == technology)
    col = np.full(n, -1)
    col[cells] = np.arange(cells.size)
    src, tgt, mass = serving.relations
    keep = (state.technology[src] == technology) & (state.technology[tgt] == technology)
    if technology == "GSM":
        keep &= state.vendor[src] == state.vendor[tgt]
    src, tgt, mass = src[keep], tgt[keep], mass[keep]
    if np.any(np.diff(src) < 0):
        raise ValueError("relations must be grouped by source")
    configured = np.array(
        [(int(s), int(t)) in neighbours for s, t in zip(src, tgt, strict=True)], dtype=bool
    )
    total = np.bincount(src, mass, n)
    conf_total = np.bincount(src, mass * configured, n)
    unconfigured = np.divide(total - conf_total, total, out=np.zeros(n), where=total > 0)
    share = np.divide(
        mass * configured, conf_total[src], out=np.zeros(src.size), where=conf_total[src] > 0
    )
    tech_columns = [
        (s, t)
        for s, t in columns
        if state.technology[s] == technology
        and state.technology[t] == technology
        and (technology == "LTE" or state.vendor[s] == state.vendor[t])
    ]
    index = {pair: i for i, pair in enumerate(tech_columns)}
    counter_col = np.array(
        [index.get((int(s), int(t)), -1) for s, t in zip(src, tgt, strict=True)], dtype=int
    )
    reported = np.array([pair in neighbours for pair in tech_columns], dtype=bool)
    return TechRelations(
        cells=cells,
        source_col=col[src],
        target_col=col[tgt],
        configured_share=share,
        counter_col=counter_col,
        unconfigured=unconfigured[cells],
        columns=tech_columns,
        reported=reported,
    )


def relation_plan(
    state: CellState,
    serving: Serving,
    neighbours: frozenset[tuple[int, int]],
    columns: list[tuple[int, int]],
) -> RelationPlan:
    """Relation plans of both technologies.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.
        neighbours: Configured neighbour relations.
        columns: Per-relation counter columns.

    Returns:
        The plan.
    """
    return RelationPlan(
        lte=tech_relations(state, serving, neighbours, columns, "LTE"),
        gsm=tech_relations(state, serving, neighbours, columns, "GSM"),
    )


def cumulative_split(totals: np.ndarray, source_col: np.ndarray, share: np.ndarray) -> np.ndarray:
    """Split per-cell integer totals over relations, keeping each cell's sum.

    Relations must be grouped by source. Counts come from rounding the
    cumulative share within each source, so they are integers and add up
    to the source's total whenever its shares add up to 1.

    Args:
        totals: Totals per period and cell, (periods, cells).
        source_col: Source column of each relation, grouped.
        share: Share of the source's total per relation, (relations,) or
            (periods, relations).

    Returns:
        Counts per period and relation.
    """
    n = source_col.size
    if n == 0:
        return np.zeros((totals.shape[0], 0))
    share = np.broadcast_to(share, (totals.shape[0], n))
    new_group = np.r_[True, source_col[1:] != source_col[:-1]]
    starts = np.flatnonzero(new_group)
    group = np.cumsum(new_group) - 1
    cumulative = np.cumsum(share, axis=1)
    before = cumulative[:, starts] - share[:, starts]
    within = cumulative - before[:, group]
    counts = np.rint(totals[:, source_col] * within)
    previous = np.zeros_like(counts)
    previous[:, 1:] = counts[:, :-1]
    previous[:, starts] = 0.0
    result: np.ndarray = counts - previous
    return result


def split_over_relations(
    rel: TechRelations, attempts: np.ndarray, success_p: np.ndarray, z: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Split each cell's handover attempts and failures over its relations.

    Args:
        rel: The technology's relations.
        attempts: Handover attempts per period and cell, (periods, cells).
        success_p: Handover success probability, broadcastable to attempts.
        z: Standard normals for the successes, (periods, cells).

    Returns:
        Attempts and successes per cell (the sums over relations), attempts
        and successes per counter column (NaN where the relation is not
        reported), and successes per demand relation.
    """
    periods = attempts.shape[0]
    att_rel = cumulative_split(attempts, rel.source_col, rel.configured_share)
    n_cells = rel.cells.size
    att_cell = np.zeros((periods, n_cells))
    np.add.at(att_cell.T, rel.source_col, att_rel.T)
    succ_cell = binomial_count(att_cell, np.broadcast_to(success_p, att_cell.shape), z)
    source_att = att_cell[:, rel.source_col]
    fail_share = np.divide(att_rel, source_att, out=np.zeros_like(att_rel), where=source_att > 0)
    fail_rel = cumulative_split(att_cell - succ_cell, rel.source_col, fail_share)
    succ_rel = att_rel - fail_rel
    n_cols = len(rel.columns)
    att_cols = np.zeros((periods, n_cols))
    succ_cols = np.zeros((periods, n_cols))
    has = rel.counter_col >= 0
    np.add.at(att_cols.T, rel.counter_col[has], att_rel[:, has].T)
    np.add.at(succ_cols.T, rel.counter_col[has], succ_rel[:, has].T)
    att_cols[:, ~rel.reported] = np.nan
    succ_cols[:, ~rel.reported] = np.nan
    return att_cell, succ_cell, att_cols, succ_cols, succ_rel


def saturating(rise_db: np.ndarray) -> np.ndarray:
    """Impairment share from an uplink noise rise, 0 at 0 dB, 1 far above.

    Args:
        rise_db: Uplink noise rise, dB.

    Returns:
        1 - exp(-rise / UL_RISE_SCALE_DB).
    """
    result: np.ndarray = 1.0 - np.exp(-np.maximum(rise_db, 0.0) / UL_RISE_SCALE_DB)
    return result


def lte_day(
    state: CellState,
    serving: Serving,
    class_load: np.ndarray,
    day: int,
    plan: RelationPlan,
    ul_rise_db: np.ndarray,
) -> DayCounters:
    """LTE counters (TS 32.425 V19.0.0 names) for one day.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.
        class_load: Activity times population shift per period for users
            living at urban, suburban and rural points, shape (periods, 3).
        day: Day index (noise seeding).
        plan: Relation plan (handover demand and configured neighbours).
        ul_rise_db: Uplink noise rise per cell (global order), dB.

    Returns:
        The day's LTE counters.
    """
    rel = plan.lte
    cells = rel.cells
    periods = class_load.shape[0]
    z = noise_block(cells, day, periods)
    active = class_load @ serving.subscribers_by_class[cells].T
    load_noise = np.exp(LOAD_NOISE_SIGMA * z[0] - LOAD_NOISE_SIGMA**2 / 2.0)
    conn = active * CONNECTED_SHARE_AT_PEAK * load_noise
    dl_share = np.where(state.band[cells] == "B40", TDD_DL_SHARE, 1.0)
    capacity = state.n_rb[cells] * 180.0 * serving.spectral_efficiency[cells] * dl_share
    capacity = np.maximum(capacity, 1.0)[None, :]
    demand = conn * DEMAND_KBPS_PER_CONNECTED
    rho = demand / capacity
    edge = serving.edge_share[cells][None, :]
    ul = saturating(ul_rise_db[cells])[None, :]
    down = state.down[cells][None, :]

    prb = 100.0 * (np.minimum(rho, 1.0) * (1.0 - PRB_OVERHEAD) + PRB_OVERHEAD)
    carried = np.minimum(demand, capacity * CARRIED_CEILING)
    volume_kbit = carried * PERIOD_S
    user_kbps = capacity * np.maximum(1.0 - rho, MIN_USER_SHARE)
    time_ms = volume_kbit / user_kbps * 1000.0

    congestion = 1.0 - np.exp(-np.maximum(0.0, rho - CONGESTION_KNEE))
    rrc_att = poisson_count(conn * PERIOD_S / MEAN_CONNECTION_S, z[1])
    rrc_p = (
        1.0
        - RRC_BASE_FAIL
        - RRC_CONGESTION_MAX_FAIL * congestion
        - RRC_EDGE_SLOPE * edge
        - UL_RRC_MAX_FAIL * ul
    )
    rrc_succ = binomial_count(rrc_att, rrc_p, z[2])
    s1_succ = binomial_count(rrc_succ, np.full(rrc_succ.shape, 1.0 - S1_BASE_FAIL), z[3])
    erab_p = (
        1.0
        - ERAB_BASE_FAIL
        - ERAB_CONGESTION_MAX_FAIL * congestion
        - ERAB_EDGE_SLOPE * edge
        - UL_ERAB_MAX_FAIL * ul
    )
    erab_succ = binomial_count(s1_succ, erab_p, z[4])
    ho_rate = np.array([HO_RATE_PER_S[a] for a in state.area_class[cells]])[None, :]
    ho_demand = conn * PERIOD_S * ho_rate
    unconfigured = rel.unconfigured[None, :]
    rlf = np.rint(ho_demand * unconfigured * RLF_WITHOUT_NEIGHBOUR)
    drop_p = DROP_BASE + DROP_EDGE_SLOPE * edge + UL_DROP_MAX * ul
    drops = binomial_count(erab_succ, drop_p, z[5]) + rlf
    ho_total = poisson_count(ho_demand * (1.0 - unconfigured), z[6])
    ho_p = HO_BASE_SUCCESS - HO_EDGE_SLOPE * edge
    ho_att, ho_succ, att_rel, succ_rel, _ = split_over_relations(rel, ho_total, ho_p, z[7])
    conn_max = np.rint(conn + 2.0 * np.sqrt(conn) * np.abs(z[1]) + 1.0)
    cqi = np.rint(
        (conn * PERIOD_S / CQI_REPORT_PERIOD_S)[:, :, None] * serving.cqi_share[cells][None, :, :]
    )
    ta = np.rint(rrc_att[:, :, None] * serving.ta_share[cells][None, :, :])
    ul_level = UL_NOISE_FLOOR_DBM + UL_LOAD_RISE_DB * np.minimum(rho, 1.0)
    ul_level = ul_level + ul_rise_db[cells][None, :]

    values = {
        "RRC.ConnEstabAtt.sum": rrc_att,
        "RRC.ConnEstabSucc.sum": rrc_succ,
        "S1SIG.ConnEstabAtt": rrc_succ,
        "S1SIG.ConnEstabSucc": s1_succ,
        "ERAB.EstabInitAttNbr.sum": s1_succ,
        "ERAB.EstabInitSuccNbr.sum": erab_succ,
        "ERAB.RelActNbr.sum": drops,
        "ERAB.SessionTimeUE": np.rint(conn * PERIOD_S),
        "RRC.ConnMean": np.rint(conn),
        "RRC.ConnMax": conn_max,
        "RRU.PrbTotDl": np.rint(prb),
        "DRB.IPVolDl.sum": np.round(volume_kbit, 1),
        "DRB.IPTimeDl.sum": np.round(time_ms, 1),
        "HO.IntraFreqOutAtt": ho_att,
        "HO.IntraFreqOutSucc": ho_succ,
        "RRU.CellUnavailableTime.sum": np.zeros(rrc_att.shape),
        "UL interference per PRB, dBm (vendor-style)": np.round(ul_level, 1),
        "CARR.WBCQIDist.Bin": cqi,
        "TA distance bins (vendor-style)": ta,
    }
    for name, array in values.items():
        values[name] = np.where(
            down if array.ndim == 2 else down[:, :, None],
            PERIOD_S if name == "RRU.CellUnavailableTime.sum" else 0.0,
            array,
        )
    down_rel = np.array([bool(state.down[s]) for s, _ in rel.columns], dtype=bool)
    relation_values = {"HO.OutAttTarget.sum": att_rel, "HO.OutSuccTarget.sum": succ_rel}
    for name, array in relation_values.items():
        relation_values[name] = np.where(down_rel[None, :] & ~np.isnan(array), 0.0, array)
    return DayCounters(cells, values, rel.columns, relation_values)


def gsm_day(
    state: CellState,
    serving: Serving,
    class_load: np.ndarray,
    day: int,
    plan: RelationPlan,
) -> DayCounters:
    """GSM counters (TS 52.402 V19.0.0 Annex B names) for one day.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.
        class_load: Activity times population shift per period for users
            living at urban, suburban and rural points, shape (periods, 3).
        day: Day index (noise seeding).
        plan: Relation plan (handover demand and configured neighbours).

    Returns:
        The day's GSM counters.
    """
    rel = plan.gsm
    cells = rel.cells
    periods = class_load.shape[0]
    z = noise_block(cells, day, periods)
    active = class_load @ serving.subscribers_by_class[cells].T
    noise = np.exp(LOAD_NOISE_SIGMA * z[0] - LOAD_NOISE_SIGMA**2 / 2.0)
    offered = active * ERLANG_PER_USER_AT_PEAK * noise
    n_tch, n_sdcch = gsm_channels(state.trx[cells])
    blocking = erlang_b(offered, np.broadcast_to(n_tch, offered.shape).astype(int))
    edge = serving.edge_share[cells][None, :]

    calls = poisson_count(offered * PERIOD_S / MEAN_HOLDING_S, z[1])
    blocked = binomial_count(calls, blocking, z[2])
    tch_att = calls - blocked
    tch_succ = binomial_count(tch_att, 1.0 - TCH_ASSIGN_FAIL - TCH_ASSIGN_EDGE_SLOPE * edge, z[3])
    sd_att = poisson_count(calls * SDCCH_PER_CALL, z[4])
    sd_offered = sd_att * SDCCH_HOLDING_S / PERIOD_S
    sd_blocking = erlang_b(sd_offered, np.broadcast_to(n_sdcch, offered.shape).astype(int))
    sd_blocked = np.rint(sd_att * sd_blocking)
    ia_succ = binomial_count(sd_att - sd_blocked, 1.0 - IA_FAIL - IA_EDGE_SLOPE * edge, z[5])
    ho_rate = np.array([GSM_HO_PER_CALL[a] for a in state.area_class[cells]])[None, :]
    ho_demand = tch_succ * ho_rate
    unconfigured = rel.unconfigured[None, :]
    rlf = np.rint(ho_demand * unconfigured * RLF_WITHOUT_NEIGHBOUR)
    tch_drop = binomial_count(tch_succ, TCH_DROP_BASE + TCH_DROP_EDGE_SLOPE * edge, z[6]) + rlf
    sd_drop = np.rint(ia_succ * (SDCCH_DROP_BASE + SDCCH_DROP_EDGE_SLOPE * edge))

    ho_total = poisson_count(ho_demand * (1.0 - unconfigured), z[7])
    ho_p = GSM_HO_BASE_SUCCESS - HO_EDGE_SLOPE * edge
    ho_att, ho_succ, att_rel, succ_rel, succ_demand = split_over_relations(
        rel, ho_total, ho_p, z[8]
    )
    ho_fail = ho_att - ho_succ
    reconnect = np.rint(ho_fail * GSM_HO_RECONNECT_SHARE)
    incoming = np.zeros_like(ho_succ)
    np.add.at(incoming.T, rel.target_col, succ_demand.T)

    values = {
        "attTCHSeizures": tch_att,
        "succTCHSeizures": tch_succ,
        "attTCHSeizuresMeetingTCHBlockedState": blocked,
        "nbrOfAvailableTCHs": np.broadcast_to(n_tch, offered.shape).astype(float),
        "meanNbrOfBusyTCHs": np.round(offered * (1.0 - blocking), 2),
        "allAvailableTCHAllocatedTime": np.round(PERIOD_S * blocking, 1),
        "attImmediateAssingProcs": sd_att,
        "succImmediateAssingProcs": ia_succ,
        "attSDCCHSeizuresMeetingSDCCHBlockedState": sd_blocked,
        "nbrOfAvailableSDCCHs": np.broadcast_to(n_sdcch, offered.shape).astype(float),
        "meanNbrOfBusySDCCHs": np.round(sd_offered * (1.0 - sd_blocking), 2),
        "nbrOfLostRadioLinksTCH": tch_drop,
        "nbrOfLostRadioLinksSDCCH": sd_drop,
        "attOutgoingInternalInterCellHDOs": ho_att,
        "succOutgoingInternalInterCellHDOs": ho_succ,
        "unsuccHDOsWithReconnection": reconnect,
        "unsuccHDOsWithLossOfConnection": ho_fail - reconnect,
        "succIncomingInternalInterCellHDOs": incoming,
    }
    down = state.down[cells][None, :]
    for name, array in values.items():
        values[name] = np.where(down, 0.0, array)
    relation_values = {
        "attOutgoingInternalInterCellHDOsPerTargetCell": att_rel,
        "succOutgoingInternalInterCellHDOsPerTargetCell": succ_rel,
    }
    return DayCounters(cells, values, rel.columns, relation_values)
