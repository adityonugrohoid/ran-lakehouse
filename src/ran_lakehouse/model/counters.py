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
N_STREAMS = 8  # standard-normal streams per cell and period

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
DROP_MISSING_NEIGHBOUR_SLOPE = 0.3
HO_BASE_SUCCESS = 0.99
HO_MISSING_NEIGHBOUR_SLOPE = 0.3
HO_EDGE_SLOPE = 0.02
# Handovers per connected-user second by area class (ASSUMPTION: smaller
# cells, more handovers).
HO_RATE_PER_S = {"urban": 0.004, "suburban": 0.002, "rural": 0.001}
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
        cells: Global cell indices, the column order of every array.
        values: Measurement name to array of shape (periods, cells), or
            (periods, cells, bins) for distributions.
    """

    cells: np.ndarray
    values: dict[str, np.ndarray]


def missing_neighbour_share(
    serving: Serving, missing: frozenset[tuple[int, int]], n_cells: int
) -> np.ndarray:
    """Share of each cell's handover overlap that lacks a neighbour relation.

    Args:
        serving: The serving summary with the relations.
        missing: (source, target) relations removed from the neighbour lists.
        n_cells: Number of cells.

    Returns:
        Share per cell, 0 when nothing is missing.
    """
    src, tgt, mass = serving.relations
    total = np.bincount(src, mass, n_cells)
    gone = np.array([(int(s), int(t)) in missing for s, t in zip(src, tgt, strict=True)])
    lost = np.bincount(src, mass * gone, n_cells) if gone.size else np.zeros(n_cells)
    result: np.ndarray = np.divide(lost, total, out=np.zeros(n_cells), where=total > 0)
    return result


def lte_day(
    state: CellState,
    serving: Serving,
    activity: np.ndarray,
    day: int,
    missing_share: np.ndarray,
    load_factor: np.ndarray,
) -> DayCounters:
    """LTE counters (TS 32.425 V19.0.0 names) for one day.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.
        activity: Activity factor per period of the day.
        day: Day index (noise seeding).
        missing_share: Missing-neighbour share per cell (global order).
        load_factor: Extra load multiplier per period and cell (global
            order), 1 for no change; shape (periods, cells).

    Returns:
        The day's LTE counters.
    """
    cells = np.flatnonzero(state.technology == "LTE")
    periods = activity.size
    z = noise_block(cells, day, periods)
    subs = serving.subscribers[cells][None, :]
    load_noise = np.exp(LOAD_NOISE_SIGMA * z[0] - LOAD_NOISE_SIGMA**2 / 2.0)
    conn = subs * CONNECTED_SHARE_AT_PEAK * activity[:, None] * load_noise * load_factor[:, cells]
    dl_share = np.where(state.band[cells] == "B40", TDD_DL_SHARE, 1.0)
    capacity = state.n_rb[cells] * 180.0 * serving.spectral_efficiency[cells] * dl_share
    capacity = np.maximum(capacity, 1.0)[None, :]
    demand = conn * DEMAND_KBPS_PER_CONNECTED
    rho = demand / capacity
    edge = serving.edge_share[cells][None, :]
    missing = missing_share[cells][None, :]
    down = state.down[cells][None, :]

    prb = 100.0 * (np.minimum(rho, 1.0) * (1.0 - PRB_OVERHEAD) + PRB_OVERHEAD)
    carried = np.minimum(demand, capacity * CARRIED_CEILING)
    volume_kbit = carried * PERIOD_S
    user_kbps = capacity * np.maximum(1.0 - rho, MIN_USER_SHARE)
    time_ms = volume_kbit / user_kbps * 1000.0

    congestion = 1.0 - np.exp(-np.maximum(0.0, rho - CONGESTION_KNEE))
    rrc_att = poisson_count(conn * PERIOD_S / MEAN_CONNECTION_S, z[1])
    rrc_p = 1.0 - RRC_BASE_FAIL - RRC_CONGESTION_MAX_FAIL * congestion - RRC_EDGE_SLOPE * edge
    rrc_succ = binomial_count(rrc_att, rrc_p, z[2])
    s1_succ = binomial_count(rrc_succ, np.full(rrc_succ.shape, 1.0 - S1_BASE_FAIL), z[3])
    erab_p = 1.0 - ERAB_BASE_FAIL - ERAB_CONGESTION_MAX_FAIL * congestion - ERAB_EDGE_SLOPE * edge
    erab_succ = binomial_count(s1_succ, erab_p, z[4])
    drop_p = DROP_BASE + DROP_EDGE_SLOPE * edge + DROP_MISSING_NEIGHBOUR_SLOPE * missing
    drops = binomial_count(erab_succ, drop_p, z[5])
    ho_rate = np.array([HO_RATE_PER_S[a] for a in state.area_class[cells]])[None, :]
    ho_att = poisson_count(conn * PERIOD_S * ho_rate, z[6])
    ho_p = HO_BASE_SUCCESS - HO_MISSING_NEIGHBOUR_SLOPE * missing - HO_EDGE_SLOPE * edge
    ho_succ = binomial_count(ho_att, ho_p, z[7])
    conn_max = np.rint(conn + 2.0 * np.sqrt(conn) * np.abs(z[1]) + 1.0)
    cqi = np.rint(
        (conn * PERIOD_S / CQI_REPORT_PERIOD_S)[:, :, None] * serving.cqi_share[cells][None, :, :]
    )
    ta = np.rint(rrc_att[:, :, None] * serving.ta_share[cells][None, :, :])

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
        "CARR.WBCQIDist.Bin": cqi,
        "TA distance bins (vendor-style)": ta,
    }
    for name, array in values.items():
        values[name] = np.where(
            down if array.ndim == 2 else down[:, :, None],
            PERIOD_S if name == "RRU.CellUnavailableTime.sum" else 0.0,
            array,
        )
    return DayCounters(cells=cells, values=values)


def gsm_day(
    state: CellState,
    serving: Serving,
    activity: np.ndarray,
    day: int,
    missing_share: np.ndarray,
    load_factor: np.ndarray,
) -> DayCounters:
    """GSM counters (TS 52.402 V19.0.0 Annex B names) for one day.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.
        activity: Activity factor per period of the day.
        day: Day index (noise seeding).
        missing_share: Missing-neighbour share per cell (global order).
        load_factor: Extra load multiplier per period and cell (global
            order), 1 for no change; shape (periods, cells).

    Returns:
        The day's GSM counters.
    """
    cells = np.flatnonzero(state.technology == "GSM")
    periods = activity.size
    z = noise_block(cells, day, periods)
    users = serving.subscribers[cells][None, :]
    noise = np.exp(LOAD_NOISE_SIGMA * z[0] - LOAD_NOISE_SIGMA**2 / 2.0)
    offered = users * ERLANG_PER_USER_AT_PEAK * activity[:, None] * noise * load_factor[:, cells]
    n_tch, n_sdcch = gsm_channels(state.trx[cells])
    blocking = erlang_b(offered, np.broadcast_to(n_tch, offered.shape).astype(int))
    edge = serving.edge_share[cells][None, :]
    missing = missing_share[cells][None, :]

    calls = poisson_count(offered * PERIOD_S / MEAN_HOLDING_S, z[1])
    blocked = binomial_count(calls, blocking, z[2])
    tch_att = calls - blocked
    tch_succ = binomial_count(tch_att, 1.0 - TCH_ASSIGN_FAIL - TCH_ASSIGN_EDGE_SLOPE * edge, z[3])
    sd_att = poisson_count(calls * SDCCH_PER_CALL, z[4])
    sd_offered = sd_att * SDCCH_HOLDING_S / PERIOD_S
    sd_blocking = erlang_b(sd_offered, np.broadcast_to(n_sdcch, offered.shape).astype(int))
    sd_blocked = np.rint(sd_att * sd_blocking)
    ia_succ = binomial_count(sd_att - sd_blocked, 1.0 - IA_FAIL - IA_EDGE_SLOPE * edge, z[5])
    tch_drop = binomial_count(
        tch_succ,
        TCH_DROP_BASE + TCH_DROP_EDGE_SLOPE * edge + DROP_MISSING_NEIGHBOUR_SLOPE * missing,
        z[6],
    )
    sd_drop = np.rint(ia_succ * (SDCCH_DROP_BASE + SDCCH_DROP_EDGE_SLOPE * edge))

    internal = internal_relation_share(state, serving, cells)
    ho_rate = np.array([GSM_HO_PER_CALL[a] for a in state.area_class[cells]])[None, :]
    ho_att = poisson_count(tch_succ * ho_rate * internal[None, :], z[7])
    ho_p = GSM_HO_BASE_SUCCESS - HO_MISSING_NEIGHBOUR_SLOPE * missing - HO_EDGE_SLOPE * edge
    ho_succ = np.clip(np.rint(ho_att * ho_p), 0, ho_att)
    ho_fail = ho_att - ho_succ
    reconnect = np.rint(ho_fail * GSM_HO_RECONNECT_SHARE)
    incoming = incoming_handovers(state, serving, cells, ho_succ)

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
    return DayCounters(cells=cells, values=values)


def internal_relation_share(state: CellState, serving: Serving, cells: np.ndarray) -> np.ndarray:
    """Share of each GSM cell's relation mass towards cells on the same BSC.

    TS 52.402 counts only handovers between cells of the same BSC as
    internal; the BSC follows the vendor region here.

    Args:
        state: Cell parameters.
        serving: The serving summary with the relations.
        cells: Global indices of the GSM cells.

    Returns:
        Share per GSM cell, in the order of cells.
    """
    src, tgt, mass = serving.relations
    n = len(state.cell_names)
    same = state.vendor[src] == state.vendor[tgt]
    total = np.bincount(src, mass, n)[cells]
    inside = np.bincount(src, mass * same, n)[cells]
    result: np.ndarray = np.divide(inside, total, out=np.zeros(cells.size), where=total > 0)
    return result


def incoming_handovers(
    state: CellState, serving: Serving, cells: np.ndarray, outgoing: np.ndarray
) -> np.ndarray:
    """Successful incoming internal handovers from neighbours' outgoing ones.

    Args:
        state: Cell parameters.
        serving: The serving summary with the relations.
        cells: Global indices of the GSM cells (columns of outgoing).
        outgoing: Successful outgoing internal handovers, (periods, cells).

    Returns:
        Incoming handovers, same shape.
    """
    src, tgt, mass = serving.relations
    n = len(state.cell_names)
    keep = (state.vendor[src] == state.vendor[tgt]) & (state.technology[src] == "GSM")
    src, tgt, mass = src[keep], tgt[keep], mass[keep]
    total = np.bincount(src, mass, n)
    column = np.full(n, -1)
    column[cells] = np.arange(cells.size)
    share = mass / total[src]
    incoming = np.zeros_like(outgoing)
    for s, t, w in zip(column[src], column[tgt], share, strict=True):
        incoming[:, t] += outgoing[:, s] * w
    result: np.ndarray = np.rint(incoming)
    return result
