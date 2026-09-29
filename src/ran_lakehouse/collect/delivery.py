"""Delivery of PM files from the simulated EMS to landing (rules P7, D1-D5).

Every file is delivered a few minutes after its period ends. The planted
delivery anomalies are drawn from the seed (rule W1) and go to the
evaluation-only answers (rule A3):

- D1 late file: delivered hours late, after its successors;
- D2 duplicate: the same content delivered twice, or a second file with the
  same name and different content (a corrected re-export of one network
  element);
- D3 missing file: never delivered;
- D4 suspect data: an interrupted collection on one network element, its
  values partial and flagged suspect (Huawei-style only: the OMeS-shaped
  format has no suspect flag);
- D5 counter rename: a software upgrade moves part of the Huawei-style
  eNodeBs to dictionary release HW-R2 at a set time.

Counts, delays and dates are START values.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from ran_lakehouse.files.dialects import HUAWEI_R2, Dialect
from ran_lakehouse.files.ems import Ems
from ran_lakehouse.model import RUN_START, NetworkModel
from ran_lakehouse.seeds import Purpose, rng

PERIOD = timedelta(minutes=15)
PERIODS_PER_DAY = 96
NORMAL_DELAY_MIN = (2.0, 8.0)  # START
LATE_DELAY_MIN = (60.0, 240.0)  # D1 (START)
REDELIVERY_DELAY_MIN = (5.0, 90.0)  # D2 second copy (START)
CORRECTION_FACTOR = (0.95, 1.05)  # D2 different content (START)
COLLECTED_SHARE = (0.3, 0.9)  # D4 share of the period collected (START)
PER_WEEK = {"D1": 3, "D2_same": 2, "D2_conflict": 1, "D3": 1, "D4": 2}  # per EMS (START)
UPGRADE_DAY = 37  # D5: day of the run (week 6, Wednesday) (START)
UPGRADE_HOUR = 2  # maintenance window, local time
UPGRADE_SHARE = 0.5  # share of the Huawei-style eNodeBs upgraded (START)


@dataclass(frozen=True)
class Anomaly:
    """One planted delivery anomaly (evaluation-only).

    Attributes:
        kind: D1, D2_same, D2_conflict, D3, D4 or D5.
        ems_id: The EMS.
        period: Global period index from RUN_START (-1 for D5).
        element: Managed element concerned (D2_conflict, D4, D5), else "".
        detail: What was done, in plain words.
    """

    kind: str
    ems_id: str
    period: int
    element: str
    detail: str


@dataclass(frozen=True)
class Delivery:
    """One file delivery to landing.

    Attributes:
        arrival: Local (WIB) arrival time.
        ems_id: The EMS.
        period: Global period index.
        variant: False for the file as written; True for the D2 corrected
            re-export.
    """

    arrival: datetime
    ems_id: str
    period: int
    variant: bool


@dataclass(frozen=True)
class DeliveryPlan:
    """How every file of a run is delivered.

    Attributes:
        anomalies: Planted anomalies, the evaluation-only answers.
        upgrade_time: D5 upgrade time (local WIB).
        upgraded: Managed elements moved to HW-R2 at upgrade_time.
        index: (kind, EMS) to period to anomaly.
    """

    anomalies: list[Anomaly]
    upgrade_time: datetime
    upgraded: frozenset[str]
    index: dict[tuple[str, str], dict[int, Anomaly]]

    def of(self, kind: str, ems_id: str) -> dict[int, Anomaly]:
        """Anomalies of one kind for one EMS, by period.

        Args:
            kind: Anomaly kind.
            ems_id: The EMS.

        Returns:
            Period to anomaly (empty when there are none).
        """
        return self.index.get((kind, ems_id), {})


def region_elements(model: NetworkModel, ems: Ems, technology: str) -> list[str]:
    """Managed elements of an EMS's region, optionally of one technology.

    Args:
        model: The network.
        ems: The EMS.
        technology: "LTE", "GSM" or "" for both.

    Returns:
        Sorted element names.
    """
    return sorted(
        {
            c.managed_element
            for c, vendor in zip(model.world.cells, model.state.vendor, strict=True)
            if vendor == ems.dialect.vendor and (not technology or c.technology == technology)
        }
    )


def plan_delivery(model: NetworkModel, ems_list: list[Ems], n_days: int) -> DeliveryPlan:
    """Draw the delivery anomalies of a run.

    Args:
        model: The network as built.
        ems_list: The EMS.
        n_days: Days in the run.

    Returns:
        The plan.
    """
    anomalies: list[Anomaly] = []
    weeks = n_days // 7
    for e_index, ems in enumerate(ems_list):
        elements = region_elements(model, ems, "")
        taken: set[int] = set()
        for week in range(weeks):
            stream = rng(Purpose.DELIVERY, e_index * 1000 + week)
            first = week * 7 * PERIODS_PER_DAY
            for kind, count in PER_WEEK.items():
                if kind == "D4" and ems.file_format != "3gpp-xml":
                    continue
                for _ in range(count):
                    period = int(stream.integers(first, first + 7 * PERIODS_PER_DAY))
                    while period in taken:
                        period = int(stream.integers(first, first + 7 * PERIODS_PER_DAY))
                    taken.add(period)
                    element = ""
                    if kind in ("D2_conflict", "D4"):
                        element = elements[int(stream.integers(len(elements)))]
                    anomalies.append(
                        Anomaly(kind, ems.ems_id, period, element, describe(kind, stream))
                    )
    huawei = [e for e in ems_list if e.dialect.vendor == HUAWEI_R2.vendor]
    upgraded: set[str] = set()
    upgrade_time = RUN_START + timedelta(days=UPGRADE_DAY, hours=UPGRADE_HOUR)
    if huawei and n_days > UPGRADE_DAY:
        enbs = region_elements(model, huawei[0], "LTE")
        pick = rng(Purpose.DELIVERY, 999_999).permutation(len(enbs))
        upgraded = {enbs[i] for i in sorted(pick[: round(UPGRADE_SHARE * len(enbs))])}
        anomalies += [
            Anomaly(
                "D5",
                huawei[0].ems_id,
                -1,
                element,
                f"software upgrade to {HUAWEI_R2.release} at {upgrade_time.isoformat()}",
            )
            for element in sorted(upgraded)
        ]
    index: dict[tuple[str, str], dict[int, Anomaly]] = {}
    for a in anomalies:
        index.setdefault((a.kind, a.ems_id), {})[a.period] = a
    return DeliveryPlan(anomalies, upgrade_time, frozenset(upgraded), index)


def describe(kind: str, stream: np.random.Generator) -> str:
    """Draw an anomaly's parameters and describe them.

    Args:
        kind: Anomaly kind.
        stream: Random stream of the week.

    Returns:
        Parameters in words, parsed back by parameter().
    """
    if kind == "D1":
        return f"delay_min={stream.uniform(*LATE_DELAY_MIN):.0f}"
    if kind == "D2_same":
        return f"redelivery_min={stream.uniform(*REDELIVERY_DELAY_MIN):.0f}"
    if kind == "D2_conflict":
        factor = stream.uniform(*CORRECTION_FACTOR)
        return f"redelivery_min={stream.uniform(*REDELIVERY_DELAY_MIN):.0f} factor={factor:.3f}"
    if kind == "D4":
        return f"collected_share={stream.uniform(*COLLECTED_SHARE):.2f}"
    return "never delivered"


def parameter(anomaly: Anomaly, name: str) -> float:
    """A numeric parameter of an anomaly.

    Args:
        anomaly: The anomaly.
        name: Parameter name.

    Returns:
        Its value.

    Raises:
        KeyError: If the anomaly has no such parameter.
    """
    for part in anomaly.detail.split():
        key, _, value = part.partition("=")
        if key == name:
            return float(value)
    raise KeyError(f"{anomaly.kind} has no parameter {name}")


def release_of(plan: DeliveryPlan, ems: Ems, element: str, start: datetime) -> Dialect:
    """The dictionary release an element's file declares for a period.

    Args:
        plan: The delivery plan.
        ems: The EMS.
        element: Managed element.
        start: Local period start.

    Returns:
        HW-R2 for upgraded elements from the upgrade on, else the EMS's base
        release.
    """
    if element in plan.upgraded and start >= plan.upgrade_time:
        return HUAWEI_R2
    return ems.dialect


def deliveries(plan: DeliveryPlan, ems_index: int, ems: Ems, period: int) -> list[Delivery]:
    """Deliveries of one EMS's file for one period.

    Args:
        plan: The delivery plan.
        ems_index: Position of the EMS (seeding).
        ems: The EMS.
        period: Global period index.

    Returns:
        Zero (D3), one, or two (D2) deliveries.
    """
    if period in plan.of("D3", ems.ems_id):
        return []
    end = RUN_START + PERIOD * (period + 1)
    stream = rng(Purpose.DELIVERY, 10_000_000 + ems_index * 1_000_000 + period)
    arrival = end + timedelta(minutes=float(stream.uniform(*NORMAL_DELAY_MIN)))
    late = plan.of("D1", ems.ems_id).get(period)
    if late is not None:
        arrival = end + timedelta(minutes=parameter(late, "delay_min"))
    out = [Delivery(arrival, ems.ems_id, period, False)]
    for kind, variant in (("D2_same", False), ("D2_conflict", True)):
        again = plan.of(kind, ems.ems_id).get(period)
        if again is not None:
            later = arrival + timedelta(minutes=parameter(again, "redelivery_min"))
            out.append(Delivery(later, ems.ems_id, period, variant))
    return out
