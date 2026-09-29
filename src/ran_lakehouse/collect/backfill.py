"""Backfill and live run (rules P7, P8, L1): simulate, deliver, collect into bronze.

Day by day: simulate the network with its planted faults, render every
EMS's PM files (with the delivery plan's dictionary releases, interrupted
collections and corrected re-exports), deliver them and the CM and FM
exports to landing at their arrival times, and let the collector load
bronze in arrival order. Only one simulated day is alive at a time. A
backfill delivers as fast as it can; a live run holds each delivery until
a real or accelerated clock reaches its arrival time.
"""

import heapq
import json
import logging
import resource
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

from ran_lakehouse.collect.collector import Collector
from ran_lakehouse.collect.delivery import (
    DeliveryPlan,
    deliveries,
    parameter,
    plan_delivery,
    release_of,
)
from ran_lakehouse.faults.plant import Fault, plan_faults
from ran_lakehouse.faults.simulate import simulate_with_faults
from ran_lakehouse.files.cmfm import day_exports
from ran_lakehouse.files.dialects import HUAWEI_R1, NOKIA_R1
from ran_lakehouse.files.ems import WIB, Adjustment, Ems, prepare_day, render_period
from ran_lakehouse.lake.bronze import (
    EVALUATION,
    append,
    create_tables,
    require_empty,
    require_started,
)
from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.model import RUN_START, Day, NetworkModel, default_model
from ran_lakehouse.world import build_world

logger = logging.getLogger(__name__)

EMS_LIST = [
    Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS", "3gpp-xml"),
    Ems("EMS-NK-01", NOKIA_R1, UTC, "Nokia-style synthetic EMS", "omes"),
]
# CM snapshot of a day delivered at 00:20 local that day; the day's change
# log and alarm log at 00:25 the next day (ASSUMPTION).
CM_SNAPSHOT_DELAY = timedelta(minutes=20)
CM_LOG_DELAY = timedelta(days=1, minutes=25)


@dataclass(order=True)
class Event:
    """A file waiting for its arrival time.

    Attributes:
        arrival: Aware arrival time.
        seq: Tie-breaker keeping submission order.
        ems: EMS name.
        name: File name.
        content: File bytes.
    """

    arrival: datetime
    seq: int
    ems: str
    name: str
    content: bytes


def anomalies_table(plan: DeliveryPlan) -> pa.Table:
    """The evaluation-only answers of the delivery plan (rule A3).

    Args:
        plan: The delivery plan.

    Returns:
        Rows of evaluation.delivery_anomalies.
    """
    starts = [
        None if a.period < 0 else (RUN_START + timedelta(minutes=15 * a.period)).replace(tzinfo=WIB)
        for a in plan.anomalies
    ]
    return pa.table(
        {
            "kind": [a.kind for a in plan.anomalies],
            "ems": [a.ems_id for a in plan.anomalies],
            "period_start": pa.array(starts, pa.timestamp("us", tz="UTC")),
            "managed_element": [a.element for a in plan.anomalies],
            "detail": [a.detail for a in plan.anomalies],
        }
    )


def render_day(
    base: NetworkModel, faults: list[Fault], plan: DeliveryPlan, day: Day, seq: int
) -> tuple[list[Event], int]:
    """Render and schedule one day's PM, CM and FM deliveries.

    Args:
        base: The network as built.
        faults: The fault schedule.
        plan: The delivery plan.
        day: The simulated day.
        seq: Next event sequence number.

    Returns:
        The day's events and files rendered.
    """
    events: list[Event] = []
    rendered = 0
    for index, ems in enumerate(EMS_LIST):
        prepared = prepare_day(base, day, ems)
        elements = [element for element, _ in prepared.elements]
        conflicts = plan.of("D2_conflict", ems.ems_id)
        interrupted = plan.of("D4", ems.ems_id)
        for p, start in enumerate(day.starts):
            period = day.index * 96 + p
            planned = deliveries(plan, index, ems, period)
            if not planned:
                continue
            releases = {e: release_of(plan, ems, e, start) for e in elements}
            adjust: dict[str, Adjustment] = {}
            if period in interrupted:
                d4 = interrupted[period]
                adjust[d4.element] = Adjustment(parameter(d4, "collected_share"), True)
            name, content = render_period(prepared, p, releases, adjust)
            rendered += 1
            for delivery in planned:
                body = content
                if delivery.variant:
                    fix = conflicts[period]
                    changed = adjust | {fix.element: Adjustment(parameter(fix, "factor"), False)}
                    body = render_period(prepared, p, releases, changed)[1]
                    rendered += 1
                arrival = delivery.arrival.replace(tzinfo=WIB)
                events.append(Event(arrival, seq, ems.ems_id, name, body))
                seq += 1
        midnight = datetime.combine(day.starts[0].date(), datetime.min.time())
        for name, content, _ in day_exports(base, faults, ems, midnight.date()):
            delay = CM_SNAPSHOT_DELAY if name.startswith("CM_") else CM_LOG_DELAY
            events.append(
                Event((midnight + delay).replace(tzinfo=WIB), seq, ems.ems_id, name, content)
            )
            seq += 1
    return events, rendered


@dataclass(frozen=True)
class Clock:
    """Pacing of a live run (rule P8).

    Attributes:
        speedup: Simulated seconds per wall-clock second; 1 is the real
            15-minute cadence, anything else is accelerated.
    """

    speedup: float

    @property
    def label(self) -> str:
        """How the clock is labelled wherever the run is shown (rule P8).

        Returns:
            "real-time" or "accelerated xN", both on simulated dates.
        """
        if self.speedup == 1:
            return "real-time (15-minute cadence, simulated dates)"
        return f"accelerated x{self.speedup:g} (simulated dates)"


@dataclass
class Pacer:
    """Holds deliveries back until the paced clock reaches their arrival.

    Attributes:
        clock: The clock.
        status: Status file rewritten after every delivery.
        sim_start: Simulated time at wall_start (aware), set by start().
        wall_start: time.monotonic() at the start, set by start().
    """

    clock: Clock
    status: Path
    sim_start: datetime = field(init=False)
    wall_start: float = field(init=False)

    def start(self, sim_start: datetime) -> None:
        """Start the clock.

        Args:
            sim_start: Aware simulated time now.
        """
        self.sim_start = sim_start
        self.wall_start = time.monotonic()
        self.status.parent.mkdir(parents=True, exist_ok=True)
        logger.info("clock: %s", self.clock.label)

    def now(self) -> datetime:
        """The simulated time now.

        Returns:
            Aware simulated time.
        """
        elapsed = (time.monotonic() - self.wall_start) * self.clock.speedup
        return self.sim_start + timedelta(seconds=elapsed)

    def wait(self, arrival: datetime) -> None:
        """Sleep until the simulated clock reaches an arrival time.

        Args:
            arrival: Aware arrival time.
        """
        ahead = (arrival - self.now()).total_seconds()
        if ahead > 0:
            time.sleep(ahead / self.clock.speedup)

    def report(self, collector: Collector) -> None:
        """Rewrite the status file.

        Args:
            collector: The collector, for its counts.
        """
        status = {
            "clock": self.clock.label,
            "accelerated": self.clock.speedup != 1,
            "speedup": self.clock.speedup,
            "simulated_time": self.now().isoformat(),
            "wall_time": datetime.now(UTC).isoformat(),
            "collector": collector.stats,
        }
        self.status.write_text(json.dumps(status, indent=2) + "\n")


def drive(
    profile: str,
    weeks: int,
    first_day: int,
    n_days: int,
    warehouse: str,
    landing: Path,
    pacer: Pacer | None,
) -> dict[str, Any]:
    """Simulate days of a run, deliver their files and collect them into bronze.

    The run's fault and delivery plans are drawn for all its weeks, so a
    live run continuing a backfill sees the same plans. A run starting at
    day 0 needs an empty bronze; a later start continues the run in the
    warehouse. Loads are idempotent by file hash.

    Args:
        profile: World profile.
        weeks: Weeks in the run (the plans' length).
        first_day: First day to simulate.
        n_days: Days to simulate.
        warehouse: Lakekeeper warehouse.
        landing: Landing root; files land in <landing>/<warehouse>/<EMS>/.
        pacer: None to deliver as fast as possible (backfill), else the
            paced clock.

    Returns:
        Counts and timings of the run.

    Raises:
        ValueError: If the days fall outside the run.
    """
    if first_day < 0 or n_days < 1 or first_day + n_days > 7 * weeks:
        raise ValueError(f"days {first_day}..{first_day + n_days - 1} are outside {weeks} weeks")
    started = time.perf_counter()
    base = default_model(build_world(profile))
    faults = plan_faults(base, weeks)
    plan = plan_delivery(base, EMS_LIST, 7 * weeks)
    con = connect(warehouse)
    create_tables(con)
    if first_day == 0:
        require_empty(con)
        append(con, f"{EVALUATION}.delivery_anomalies", anomalies_table(plan))
    else:
        require_started(con, len(plan.anomalies))
    kind = "backfill" if pacer is None else "run"
    load_id = f"{kind}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    collector = Collector(con, landing / warehouse, load_id)

    def release(e: Event) -> None:
        if pacer is not None:
            pacer.wait(e.arrival)
        collector.receive(e.ems, e.name, e.content, e.arrival)
        if pacer is not None:
            collector.flush()
            pacer.report(collector)

    queue: list[Event] = []
    seq = 0
    timings = {"simulate": 0.0, "render": 0.0, "collect": 0.0}
    rendered = 0
    if pacer is not None:
        pacer.start((RUN_START + timedelta(days=first_day)).replace(tzinfo=WIB))
    t0 = time.perf_counter()
    for day in simulate_with_faults(base, faults, first_day, n_days):
        t1 = time.perf_counter()
        timings["simulate"] += t1 - t0
        events, count = render_day(base, faults, plan, day, seq)
        seq += len(events)
        rendered += count
        for event in events:
            heapq.heappush(queue, event)
        t2 = time.perf_counter()
        timings["render"] += t2 - t1
        day_end = (RUN_START + timedelta(days=day.index + 1)).replace(tzinfo=WIB)
        while queue and queue[0].arrival < day_end:
            release(heapq.heappop(queue))
        collector.retain(day_end)
        t0 = time.perf_counter()
        timings["collect"] += t0 - t2
        logger.info("%s day %d: %s", kind, day.index, collector.stats)
    t2 = time.perf_counter()
    while queue:
        release(heapq.heappop(queue))
    collector.flush()
    timings["collect"] += time.perf_counter() - t2
    in_range = [a for a in plan.anomalies if a.period < 0 or a.period // 96 < first_day + n_days]
    return {
        "profile": profile,
        "run_weeks": weeks,
        "first_day": first_day,
        "days": n_days,
        "warehouse": warehouse,
        "clock": "none: as fast as possible" if pacer is None else pacer.clock.label,
        "files_rendered": rendered,
        "collector": collector.stats,
        "anomalies_planted": {
            k: sum(a.kind == k for a in in_range)
            for k in ("D1", "D2_same", "D2_conflict", "D3", "D4", "D5")
        },
        "timing_s": {k: round(v, 1) for k, v in timings.items()}
        | {"total": round(time.perf_counter() - started, 1)},
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0),
    }
