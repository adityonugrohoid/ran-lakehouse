# Configuration and alarm report

Synthetic network. 84 days of CM snapshots, CM change log and FM alarm
log from both simulated EMS (rules C1, C2): object classes in the solution-set
spellings (TS 28.659, TS 28.656), DNs per TS 32.300 in each EMS's naming style, alarm
notifications with the TS 32.111-2 V19.0.0 fields. The exports trace the planted
faults, so only counts are reported here. Written by
`python -m ran_lakehouse.files.oss_report` from `config_alarms.json`.

## EMS-HW-01 (3GPP (TS 32.300) DNs, +07:00 local)

| Snapshot objects on the first day | Count |
|---|---|
| EUtranCellFDD | 852 |
| EUtranCellTDD | 150 |
| EUtranRelation | 3,365 |
| GsmCell | 132 |
| GsmRelation | 710 |

Snapshot rows per day: 5,208 to 5,209 (neighbour relations come and go).

| Change log entries by attribute | Count |
|---|---|
| electricalTiltDeg | 13 |
| relation | 4 |
| txPowerDbm | 8 |

| Alarm notifications by type and probable cause | Count |
|---|---|
| notifyClearedAlarm: Degraded Signal | 3 |
| notifyClearedAlarm: Transmitter Failure | 4 |
| notifyNewAlarm: Degraded Signal | 3 |
| notifyNewAlarm: Transmitter Failure | 4 |

| Consistency with the planted schedule | Exported | Implied |
|---|---|---|
| change log entries, exported / implied by the schedule | 25 | 25 |
| alarm notifications, exported / implied by the schedule | 14 | 14 |

| Gzip size over the run | MB |
|---|---|
| CM | 5.6 |
| CMLOG | 0.01 |
| FM | 0.0 |

## EMS-NK-01 (Nokia-style DNs, UTC)

| Snapshot objects on the first day | Count |
|---|---|
| EUtranCellFDD | 414 |
| EUtranCellTDD | 36 |
| EUtranRelation | 2,134 |
| GsmCell | 132 |
| GsmRelation | 750 |

Snapshot rows per day: 3,465 to 3,466 (neighbour relations come and go).

| Change log entries by attribute | Count |
|---|---|
| electricalTiltDeg | 2 |
| relation | 4 |
| txPowerDbm | 6 |

| Alarm notifications by type and probable cause | Count |
|---|---|
| notifyClearedAlarm: Transmitter Failure | 1 |
| notifyNewAlarm: Transmitter Failure | 1 |

| Consistency with the planted schedule | Exported | Implied |
|---|---|---|
| change log entries, exported / implied by the schedule | 12 | 12 |
| alarm notifications, exported / implied by the schedule | 2 | 2 |

| Gzip size over the run | MB |
|---|---|
| CM | 3.36 |
| CMLOG | 0.0 |
| FM | 0.0 |

## Timing

Measured on the build machine; varies run to run.

| Step | Seconds |
|---|---|
| network, schedule and 12 weeks of exports | 52.2 |
