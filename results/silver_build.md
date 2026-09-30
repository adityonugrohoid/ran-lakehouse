# Silver build report

Synthetic network, demo profile. 12 weeks of bronze PM
values built into silver one partition (a UTC day of one EMS) at a time, each built
30 minutes after its day ends from the files in by then; files
arriving later are merged into their own hour (rules L2, D1 to D5). The anomaly
answers are evaluation-only, so only counts per kind appear here. Written by
`python -m ran_lakehouse.lake.silver_report` from `silver_build.json`.

## Planted anomalies

In scope: periods inside the span of delivered files, without its first and last
period. Found: what silver flagged. Matched: found where it was planted.

| Kind | Planted | Found | Matched | Silver's handling |
|---|---|---|---|---|
| D1 | 71 | 71 | 71 | file flagged late; merged into its partition if after the build |
| D2_same | 48 | 48 | 48 | redelivery counted, loaded once |
| D2_conflict | 24 | 24 | 24 | latest version wins, changed values flagged conflict |
| D3 | 24 | 24 | 24 | gap per network element and period, no row, never zero |
| D4 | 24 | 24 | 24 | suspect flag carried per value |
| D5 | 92 | 92 | 92 | both releases mapped, DRB.IPVolDl.sum continuous across the upgrade |

D5 counts network elements moved to the renamed release.

## Silver

| Table | Rows | Data files | MB |
|---|---|---|---|
| silver.pm_measurements | 464,354,374 | 176 | 1,618.2 |
| silver.pm_files | 15,968 | 173 | 1.9 |
| silver.pm_gaps | 4,164 | 28 | 0.1 |
| silver.counter_map | 204 | 1 | 0.0 |
| silver.loads | 173 | 173 | 0.4 |
| total | | | 1,620.5 |

| pm_measurements rows | Rows |
|---|---|
| derived | 11,592,768 |
| suspect | 4,405 |
| conflict | 1,819 |
| late | 2,204,188 |
| 15 min | 374,510,422 |
| 60 min | 89,843,952 |

Derived rows are 3GPP measurements converted from vendor-style quantities
(RRU.PrbTotDl from used PRBs, RRU.CellUnavailableTime.sum from availability samples).

| Counter map (vendor counters per release) | Counters |
|---|---|
| HW-R1: labelled vendor-style | 23 |
| HW-R1: 3GPP name | 46 |
| HW-R2: labelled vendor-style | 23 |
| HW-R2: 3GPP name | 46 |
| NK-R1: labelled vendor-style | 18 |
| NK-R1: 3GPP name | 48 |

| Loads | Count | Files | Bronze rows read | Silver rows | Seconds, total | Seconds, max |
|---|---|---|---|---|---|---|
| catch-up | 5 | 5 | 286,810 | 293,722 | 14.5 | 3.8 |
| partition | 168 | 15,963 | 453,245,164 | 464,198,538 | 2,818.4 | 60.4 |

## Cost

Measured on the build machine; varies run to run. Peak RSS is the process's
maximum resident set size during the build (DuckDB memory limit 1GB).

| Step | Seconds |
|---|---|
| silver build | 2,861.1 |
| counts, evaluation and storage | 61.2 |

Peak RSS during the build: 2,235 MB.

Iceberg commits rejected and run again: 0 (a wall-clock step
makes DuckDB 1.5.x build on a stale snapshot; see `lake.catalog.write`).
