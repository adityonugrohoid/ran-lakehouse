# PM file report

Synthetic network. One demo day written by the Huawei-style EMS as type B 3GPP PM XML
files (TS 32.435 V19.0.0 measCollecFile, names per TS 32.432 V19.0.0 clause 5.1.2,
gzip), local time with +0700 (rule W5), then parsed and structure-checked. Measured on
the build machine; times vary run to run. Written by
`python -m ran_lakehouse.files.report` from `pm_files.json`.

Sample file name: `B20260105.1200+0700-1215+0700_EMS-HW-01.xml.gz`. 96 files per day, 184 network elements per file, 1134 cells in the region, 5,857,728 values per day.

| Size | MB |
|---|---|
| gzip, total per day | 10.23 |
| gzip, mean per file | 0.107 |
| xml, total per day | 127.69 |
| gzip ratio | 12.5 |

| Speed | Value |
|---|---|
| write, s per day | 8.3 |
| write, values per s | 708,694 |
| parse and check, s per day | 12.3 |
| parse, values per s | 474,552 |

## Files in a 12-week run (rule P2)

| Layout | Files |
|---|---|
| type B, both EMS | 16,128 |
| type A, one per network element | 2,241,792 |

## Huawei-style dictionary

Release HW-R1: 69 counters in measInfos LTE.Cell, LTE.CQI, LTE.TA, LTE.NCell, GSM.Cell, GSM.NCell.

| Attestation of the counter name | Counters |
|---|---|
| ASSUMPTION: no public name found | 8 |
| public description | 30 |
| public description, weakly attested | 31 |
