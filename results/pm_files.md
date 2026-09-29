# PM file report

Synthetic network. One demo day written by both simulated EMS and read back: the
Huawei-style EMS writes type B 3GPP PM XML (TS 32.435 V19.0.0 measCollecFile, names
per TS 32.432 V19.0.0 clause 5.1.2) in local time with +0700; the Nokia-style EMS
writes the OMeS-shaped format in UTC (rules P1-P5, W5); both gzip. Measured on the
build machine; times vary run to run. Written by `python -m ran_lakehouse.files.report`
from `pm_files.json`.

## EMS-HW-01 (3gpp-xml)

Sample file name: `B20260105.1200+0700-1215+0700_EMS-HW-01.xml.gz`. 96 files per day, 184 network elements or objects per file, 1134 cells in the region, 3,621,264 values per day.

| Size | MB |
|---|---|
| gzip, total per day | 7.17 |
| gzip, mean per file | 0.075 |
| xml, total per day | 96.05 |
| gzip ratio | 13.4 |

| Speed | Value |
|---|---|
| write, s per day | 3.8 |
| write, values per s | 958,996 |
| parse and check, s per day | 6.0 |
| parse, values per s | 600,579 |

Dictionary release HW-R1: 69 counters in LTE.Cell, LTE.CQI, LTE.TA, LTE.NCell, GSM.Cell, GSM.NCell.

| Attestation of the counter name | Counters |
|---|---|
| ASSUMPTION: no public name found | 8 |
| public description | 30 |
| public description, weakly attested | 31 |

## EMS-NK-01 (omes)

Sample file name: `OMeS_EMS-NK-01_20260105T0500Z.xml.gz`. 96 files per day, 3424 network elements or objects per file, 582 cells in the region, 1,822,800 values per day.

| Size | MB |
|---|---|
| gzip, total per day | 6.87 |
| gzip, mean per file | 0.072 |
| xml, total per day | 127.97 |
| gzip ratio | 18.6 |

| Speed | Value |
|---|---|
| write, s per day | 5.1 |
| write, values per s | 357,779 |
| parse and check, s per day | 6.4 |
| parse, values per s | 285,745 |

Dictionary release NK-R1: 66 counters in LTE_Signalling, LTE_EPS_Bearer, LTE_Cell_Throughput, LTE_Cell_Resource, LTE_Cell_Load, LTE_Cell_Avail, LTE_Intra_Freq_HO, LTE_Power_Quality_UL, LTE_Quality_DL, LTE_Timing_Advance, LTE_Neighb_Cell_HO, BSC_Traffic, BSC_Handover, BSC_Adjacent_HO.

| Attestation of the counter name | Counters |
|---|---|
| ASSUMPTION: no public name found | 35 |
| public description | 28 |
| public description, weakly attested | 3 |

## Files in a 12-week run (rule P2)

| Layout | Files |
|---|---|
| one file per EMS per period (both EMS) | 16,128 |
| type A, one per network element | 2,241,792 |
