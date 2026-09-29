# Bronze backfill report

Synthetic network, demo profile. 12 weeks of PM, CM and FM
files from both simulated EMS, delivered to landing at their arrival times with the
planted delivery anomalies (rule D1 to D5), picked up by the collector and loaded into
the bronze Iceberg tables (rules P7, L1). Clock: none: as fast as possible. The anomaly
answers are evaluation-only, so only counts per kind appear here. Written by
`python -m ran_lakehouse.collect.report` from `bronze_backfill.json`.

## Delivery and collection

Files rendered: 16,128. Deliveries seen by the collector: 16,680, loaded 16,632, skipped as already loaded (same name and hash) 48.

| File kind and outcome | Deliveries |
|---|---|
| CM loaded | 168 |
| CMLOG loaded | 168 |
| FM loaded | 168 |
| PM loaded | 16,128 |
| PM skipped, same file | 48 |

| Planted delivery anomaly | Count |
|---|---|
| D1 | 72 |
| D2_conflict | 24 |
| D2_same | 48 |
| D3 | 24 |
| D4 | 24 |
| D5 | 92 |

D5 counts the network elements moved to the renamed dictionary release.

## Bronze

| Table | Rows | Data files | MB |
|---|---|---|---|
| bronze.pm_values | 457,253,306 | 527 | 1,904.8 |
| bronze.file_arrivals | 16,680 | 449 | 2.5 |
| bronze.cm_records | 728,723 | 87 | 21.0 |
| bronze.fm_records | 16 | 12 | 0.0 |
| evaluation.delivery_anomalies | 284 | 1 | 0.0 |
| total | | | 1,928.4 |

| PM rows by granularity (rule P1, P4) | Rows |
|---|---|
| 15 min | 366,554,126 |
| 60 min | 90,699,180 |

| PM rows by dictionary release (rule D5) | Rows |
|---|---|
| HW-R1 | 228,442,949 |
| HW-R2 | 75,710,677 |
| none declared (OMeS) | 153,099,680 |

Landing at the end keeps 3 days: 599 files, 43.9 MB.

## Cost

Measured on the build machine; varies run to run. Peak RSS is the process's
maximum resident set size.

| Step | Seconds |
|---|---|
| simulate | 92.2 |
| render | 818.5 |
| collect | 1,909.4 |
| total | 2,847.2 |

Peak RSS: 2,784 MB.

| Machine | |
|---|---|
| cpus | 16 |
| python | 3.12.3 |
| duckdb | 1.5.6 |
| pyiceberg | 0.12.0 |
| duckdb_memory_limit | 1GB |
