# Planted faults and what-if report

Synthetic network (rules F1-F4, M6). Every fault is a change inside the network model;
the what-if replays a week with the same random noise (common random numbers). Each kind
is demonstrated on its own cell for the first week of the run, outside the planted
schedule; the schedule itself appears only as counts and aggregate recovery, since its
answers are evaluation-only (rule A3). Written by `python -m ran_lakehouse.faults.report`
from `faults.json`.

![Primary KPI per day](faults_recovery.png)

## Fault kinds

| Kind | Cause | Change in the model (START) | Duration, h | Right answer | Primary KPI |
|---|---|---|---|---|---|
| F1a | electrical tilt changed by mistake; the cell overshoots | tilt set to 0 deg (urban cells) | 48-144 | restore tilt | E-RAB drop rate (%), area |
| F1b | neighbour relation deleted from the neighbour list | strongest neighbour relation deleted | 48-144 | add neighbour | E-RAB drop rate (%), cell |
| F1c | transmit power reduced after maintenance | power -6 dB | 48-144 | restore power | DL IP throughput (kbit/s), area |
| F1d | traffic surge in the cell's area | persons x2.5 within 0.6 km (suburban cells) | 24-72 | load-balancing offset or capacity note | DL IP throughput (kbit/s), cell |
| F1e | external uplink interference source | uplink source 0.1-0.3 km along the azimuth, raising the cell's uplink noise by 15 dB | 48-168 | no parameter fix, field visit | RRC setup success (%), cell |
| F1f | cell out of service | cell down | 3-12 | no parameter fix, alarm-driven | Cell availability (%), cell |

Recovery = (fixed - faulty) / (clean - faulty) on the primary KPI: 1 restores the clean
value, 0 does nothing, below 0 makes it worse.

## F1a: electrical tilt changed by mistake; the cell overshoots

Cell ENB0167_B3_3 (urban, huawei-style), 2026-01-05T00:00:00 to 2026-01-12T00:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 99.321 / 98.473 / 99.321 | 97.718 / 97.608 / 97.718 |
| RRC setup success (%) | 99.642 / 99.042 / 99.642 | 98.504 / 98.425 / 98.504 |
| E-RAB drop rate (%) | 0.222 / 0.686 / 0.222 | 1.063 / 1.129 / 1.063 |
| DL IP throughput (kbit/s) | 27388.017 / 17594.273 / 27388.017 | 6420.237 / 6465.108 / 6420.237 |
| Handover success (%) | 99.097 / 98.641 / 99.097 | 98.458 / 98.411 / 98.458 |
| Mean DL PRB use (%) | 10.214 / 12.637 / 10.214 | 17.646 / 17.894 / 17.646 |
| RRC connections, mean | 2.69 / 2.72 / 2.69 | 50.137 / 50.289 / 50.137 |
| Cell availability (%) | 100.0 / 100.0 / 100.0 | 100.0 / 100.0 / 100.0 |

PM traces (clean, faulty):

- subscribers served by the cell: [328, 332]
- cells touched: 10
- share of users beyond 16 TA steps: [0.0, 0.0]

CM change log:

- 2026-01-05T00:00:00 SubNetwork=RanLake,SubNetwork=West,ManagedElement=ENB0167,ENBFunction=1,EUtranCellFDD=ENB0167_B3_3 electricalTiltDeg: 8 to 0
- 2026-01-12T00:00:00 SubNetwork=RanLake,SubNetwork=West,ManagedElement=ENB0167,ENBFunction=1,EUtranCellFDD=ENB0167_B3_3 electricalTiltDeg: 0 to 8

FM alarm log:

- none

Right answer: restore tilt (what-if: tilt +2, tilt +2, tilt +2, tilt +2). Recovery (area): 1.0; over the area: 1.0.

Wrong fix, power +3 dB on the overshooting cell: recovery -0.273.

## F1b: neighbour relation deleted from the neighbour list

Cell ENB0188_B3_2 (suburban, nokia-style), 2026-01-05T00:00:00 to 2026-01-12T00:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 98.819 / 98.819 / 98.819 | 98.869 / 98.869 / 98.869 |
| RRC setup success (%) | 99.309 / 99.309 / 99.309 | 99.348 / 99.348 / 99.348 |
| E-RAB drop rate (%) | 0.522 / 1.481 / 0.522 | 0.5 / 1.039 / 0.5 |
| DL IP throughput (kbit/s) | 24013.523 / 24013.523 / 24013.523 | 24056.287 / 24056.287 / 24056.287 |
| Handover success (%) | 98.646 / 98.799 / 98.646 | 98.598 / 98.641 / 98.598 |
| Mean DL PRB use (%) | 22.333 / 22.333 / 22.333 | 20.923 / 20.923 / 20.923 |
| RRC connections, mean | 9.308 / 9.308 / 9.308 | 16.545 / 16.545 / 16.545 |
| Cell availability (%) | 100.0 / 100.0 / 100.0 | 100.0 / 100.0 / 100.0 |

PM traces (clean, faulty):

- subscribers served by the cell: [989, 989]
- cells touched: 2
- HO.OutAttTarget.sum on the deleted relation, week: [5297.0, None]

CM change log:

- 2026-01-05T00:00:00 SubNetwork=RanLake,SubNetwork=East,ManagedElement=ENB0188,ENBFunction=1,EUtranCellFDD=ENB0188_B3_2,EUtranRelation=ENB0198_B3_1 EUtranRelation: present to deleted
- 2026-01-12T00:00:00 SubNetwork=RanLake,SubNetwork=East,ManagedElement=ENB0188,ENBFunction=1,EUtranCellFDD=ENB0188_B3_2,EUtranRelation=ENB0198_B3_1 EUtranRelation: deleted to present

FM alarm log:

- none

Right answer: add neighbour (what-if: add_neighbour ENB0198_B3_1). Recovery (cell): 1.0; over the area: 1.0.

## F1c: transmit power reduced after maintenance

Cell ENB0156_B3_3 (suburban, huawei-style), 2026-01-05T00:00:00 to 2026-01-12T00:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 97.785 / 97.526 / 97.785 | 98.447 / 98.383 / 98.447 |
| RRC setup success (%) | 98.548 / 98.394 / 98.548 | 99.041 / 98.997 / 99.041 |
| E-RAB drop rate (%) | 1.096 / 1.228 / 1.096 | 0.722 / 0.767 / 0.722 |
| DL IP throughput (kbit/s) | 15127.432 / 19160.877 / 15127.432 | 15709.22 / 14243.024 / 15709.22 |
| Handover success (%) | 98.039 / 98.089 / 98.039 | 98.561 / 98.566 / 98.561 |
| Mean DL PRB use (%) | 28.161 / 14.624 / 28.161 | 21.103 / 21.377 / 21.103 |
| RRC connections, mean | 9.162 / 3.531 / 9.162 | 83.668 / 83.56 / 83.668 |
| Cell availability (%) | 100.0 / 100.0 / 100.0 | 100.0 / 100.0 / 100.0 |

PM traces (clean, faulty):

- subscribers served by the cell: [978, 373]
- cells touched: 13

CM change log:

- 2026-01-05T00:00:00 SubNetwork=RanLake,SubNetwork=West,ManagedElement=ENB0156,ENBFunction=1,EUtranCellFDD=ENB0156_B3_3 txPowerDbm: 49.0 to 43.0
- 2026-01-12T00:00:00 SubNetwork=RanLake,SubNetwork=West,ManagedElement=ENB0156,ENBFunction=1,EUtranCellFDD=ENB0156_B3_3 txPowerDbm: 43.0 to 49.0

FM alarm log:

- none

Right answer: restore power (what-if: power +3, power +3). Recovery (area): 1.0; over the area: 1.0.

Finding: the cell looks healthier while the area gets worse. With less power it serves fewer, closer users, and its former edge users load the neighbours.

## F1d: traffic surge in the cell's area

Cell ENB0156_B3_3 (suburban, huawei-style), 2026-01-05T00:00:00 to 2026-01-12T00:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 97.785 / 97.317 / 98.92 | 98.518 / 98.32 / 98.167 |
| RRC setup success (%) | 98.548 / 98.228 / 99.365 | 99.092 / 98.949 / 98.839 |
| E-RAB drop rate (%) | 1.096 / 1.031 / 0.459 | 0.684 / 0.706 / 0.799 |
| DL IP throughput (kbit/s) | 15127.432 / 3804.527 / 26072.16 | 15992.1 / 7386.743 / 7178.482 |
| Handover success (%) | 98.039 / 98.288 / not reported | 98.593 / 98.576 / 98.528 |
| Mean DL PRB use (%) | 28.161 / 52.238 / 22.022 | 20.378 / 25.361 / 25.885 |
| RRC connections, mean | 9.162 / 19.69 / 9.757 | 97.226 / 125.381 / 125.323 |
| Cell availability (%) | 100.0 / 100.0 / 100.0 | 100.0 / 100.0 / 100.0 |

PM traces (clean, faulty):

- subscribers served by the cell: [978, 2104]
- cells touched: 20

CM change log:

- none

FM alarm log:

- none

Right answer: load-balancing offset or capacity note (what-if: cio -3, cio -3). Recovery (cell): 1.967; over the area: -0.024.

Finding: the offset moves the congestion. The cell recovers past its clean value while the area gets worse, because the neighbours share the surge; the capacity note is the durable answer.

## F1e: external uplink interference source

Cell ENB0177_B3_1 (suburban, huawei-style), 2026-01-05T00:00:00 to 2026-01-12T00:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 98.318 / 68.52 / 68.52 | 98.318 / 68.52 / 68.52 |
| RRC setup success (%) | 98.929 / 75.838 / 75.838 | 98.929 / 75.838 / 75.838 |
| E-RAB drop rate (%) | 0.768 / 3.48 / 3.48 | 0.768 / 3.48 / 3.48 |
| DL IP throughput (kbit/s) | 26156.711 / 26156.711 / 26156.711 | 26156.711 / 26156.711 / 26156.711 |
| Handover success (%) | 99.098 / 99.098 / 99.098 | 99.098 / 99.098 / 99.098 |
| Mean DL PRB use (%) | 12.595 / 12.595 / 12.595 | 12.595 / 12.595 / 12.595 |
| RRC connections, mean | 3.637 / 3.637 / 3.637 | 3.637 / 3.637 / 3.637 |
| Cell availability (%) | 100.0 / 100.0 / 100.0 | 100.0 / 100.0 / 100.0 |

PM traces (clean, faulty):

- subscribers served by the cell: [378, 378]
- cells touched: 1
- uplink noise rise at the cell, dB: 15.0
- UL interference per PRB, dBm (vendor-style), week mean: [-116.2, -101.2]

CM change log:

- none

FM alarm log:

- raised 2026-01-05T00:00:00, cleared 2026-01-12T00:00:00: Communications Alarm, Degraded Signal, Major, "uplink interference detected"

Right answer: no parameter fix, field visit. Recovery (cell): 0.0; over the area: 0.0.

Every single bounded parameter change on the cell:

- tilt +2: recovery 0.005
- tilt -2: recovery -0.004
- power +3: recovery 0.003
- power -3: recovery -0.001
- cio -3: recovery 0.005
- cio +3: recovery -0.018

## F1f: cell out of service

Cell ENB0156_B3_3 (suburban, huawei-style), 2026-01-05T00:00:00 to 2026-01-05T06:00:00.

PM, KPIs over the week (clean / faulty / fixed):

| KPI | Cell | Area (cell and touched cells) |
|---|---|---|
| E-RAB accessibility (%) | 97.785 / 97.784 / 97.784 | 98.447 / 98.447 / 98.447 |
| RRC setup success (%) | 98.548 / 98.548 / 98.548 | 99.041 / 99.041 / 99.041 |
| E-RAB drop rate (%) | 1.096 / 1.099 / 1.099 | 0.722 / 0.722 / 0.722 |
| DL IP throughput (kbit/s) | 15127.432 / 15073.221 / 15073.221 | 15709.22 / 15708.712 / 15708.712 |
| Handover success (%) | 98.039 / 98.036 / 98.036 | 98.561 / 98.561 / 98.561 |
| Mean DL PRB use (%) | 28.161 / 27.726 / 27.726 | 21.103 / 21.088 / 21.088 |
| RRC connections, mean | 9.162 / 9.06 / 9.06 | 83.668 / 83.661 / 83.661 |
| Cell availability (%) | 100.0 / 96.429 / 96.429 | 100.0 / 99.725 / 99.725 |

PM traces (clean, faulty):

- subscribers served by the cell: [978, 0]
- cells touched: 13

CM change log:

- none

FM alarm log:

- raised 2026-01-05T00:00:00, cleared 2026-01-05T06:00:00: Equipment Alarm, Transmitter Failure, Critical, "cell out of service"

Right answer: no parameter fix, alarm-driven. Recovery (cell): 0.0; over the area: 0.0.

## Planted schedule (rule F3)

33 faults over 12 weeks; quiet weeks [1, 6, 7]; 47 pairs of faults overlap in time.

| Kind | Faults | Evaluated | Recovery min / median / max | Over the area |
|---|---|---|---|---|
| F1a | 8 | 8 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| F1b | 4 | 4 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| F1c | 7 | 7 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| F1d | 3 | 3 | 1.116 / 2.148 / 36.156 | -0.163 / 0.08 / 2.016 |
| F1e | 6 | 6 | -0.002 / 0.003 / 0.024 | n/a |
| F1f | 5 | 4 | 0.0 / 0.0 / 0.0 | 0.0 / 0.0 / 0.0 |

F1e counts the best single bounded parameter change; F1f has no parameter to change.
Outages of GSM cells are not evaluated here: the recovery KPIs are LTE KPIs.

| Week | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Faults | 0 | 5 | 3 | 3 | 3 | 0 | 0 | 3 | 4 | 3 | 6 | 3 |

## Reach of every planted fault (rule F1)

Each fault's first whole day replayed with and without it (same random draws). A
cell is affected when a KPI moves by more than 1 point (RRC setup success, E-RAB accessibility or drop rate; GSM service access or
TCH blocking); the faulty cell always counts. Share: the affected cells' part of
their technology's access attempts that day, which may not exceed 2% (START, tested). Faults are listed by
reach only; their cells and times are evaluation-only (rule A3).

| Kind | Cells affected | Share of access attempts (%) |
|---|---|---|
| F1d | 2 | 1.22 |
| F1e | 3 | 0.53 |
| F1e | 10 | 0.27 |
| F1e | 5 | 0.25 |
| F1e | 3 | 0.24 |
| F1e | 8 | 0.24 |
| F1f | 4 | 0.21 |
| F1c | 1 | 0.19 |
| F1f | 1 | 0.13 |
| F1d | 1 | 0.1 |
| F1a | 1 | 0.07 |
| F1f | 1 | 0.07 |
| F1c | 1 | 0.06 |
| F1e | 3 | 0.06 |
| F1b | 1 | 0.06 |
| F1a | 2 | 0.06 |
| F1d | 1 | 0.06 |
| F1c | 2 | 0.06 |
| F1c | 1 | 0.06 |
| F1b | 1 | 0.05 |
| F1c | 1 | 0.05 |
| F1f | 2 | 0.05 |
| F1a | 1 | 0.04 |
| F1c | 1 | 0.03 |
| F1b | 1 | 0.03 |
| F1b | 1 | 0.03 |
| F1a | 1 | 0.02 |
| F1a | 1 | 0.02 |
| F1a | 1 | 0.01 |
| F1c | 1 | 0.01 |
| F1a | 1 | 0.01 |
| F1a | 1 | 0.0 |
| F1f | 1 | 0.0 |

## Twelve weeks, network-wide LTE KPIs

| KPI | Clean | With faults |
|---|---|---|
| E-RAB accessibility (%) | 98.314 | 98.3 |
| RRC setup success (%) | 98.949 | 98.939 |
| E-RAB drop rate (%) | 0.559 | 0.56 |
| DL IP throughput (kbit/s) | 7296.803 | 7293.249 |
| Handover success (%) | 98.714 | 98.714 |
| Mean DL PRB use (%) | 19.005 | 19.008 |
| RRC connections, mean | 8837.903 | 8839.63 |
| Cell availability (%) | 100.0 | 99.999 |

## Timing

Measured on the build machine; varies run to run.

| Step | Seconds |
|---|---|
| demo network and fault plan | 27.4 |
| six demonstrations | 96.7 |
| recovery of every planted fault | 214.0 |
| reach of every planted fault | 31.3 |
| 12 weeks of counters with faults | 82.9 |

| Resource | Value |
|---|---|
| wall time of the whole report, s | 464.8 |
| peak resident set, MB | 1349 |
