# Time travel and lineage report

Synthetic network (rules D7, D8). Written by
`python -m ran_lakehouse.lake.lineage_report` from `time_travel_lineage.json`.

## Time travel (rule D7)

Every gold write is an Iceberg snapshot, so a KPI can be read as it was at any
earlier time: `SELECT ... FROM lk.gold.lte_kpi_hour AT (TIMESTAMP => <time>)`
(lake.timetravel.as_of). Worked example, staged on the tiny profile in a fresh warehouse:
a planted late file of EMS-HW-01 for the period from 2026-02-13T23:45:00+00:00
arrived at 2026-02-14T03:52:00+00:00 (delay_min=232),
after its UTC day had been built into silver and published in gold. Silver then merged
it into its hour and gold rebuilt the day.

| LTE_RRC_SSR v1, ENB0001_B3_1, hour from 2026-02-13T23:00:00+00:00 | Value | Coverage | Periods |
|---|---|---|---|
| as of the first publication (2026-09-30T06:42:39.909656+00:00) | 99.6310 | 0.75 | 3 |
| now | 99.7319 | 1.0 | 4 |

gold.lte_kpi_hour held 9 snapshots after the staging.

## Lineage (rule D8)

One query from a gold KPI value to every silver row it was computed from, the bronze
row each came from and the file that carried it (`ranlake lineage`, lake.lineage).
DuckDB's Iceberg catalog has no views, so the query is kept in code:

```sql
WITH k AS (
    SELECT value, coverage, suspect_share, periods_reported, periods_expected
    FROM lk.gold.{technology}_kpi_{granularity}
    WHERE cell_name = $cell AND kpi_id = $kpi AND formula_version = $version
        AND {period_column} = $period
),
c AS (SELECT ems, dn FROM lk.gold.cells WHERE cell_name = $cell),
s AS (
    SELECT m.*
    FROM lk.silver.pm_measurements m JOIN c ON m.ems = c.ems
        AND (m.object_dn = c.dn OR starts_with(m.object_dn, c.dn || ',')
            OR starts_with(m.object_dn, c.dn || '/'))
    WHERE m.period_start >= $lo AND m.period_start < $hi
        AND m.granularity_min = $granularity_min AND list_contains($measurements, m.measurement)
),
b AS (SELECT s.*, unnest(string_split(s.vendor_counter, ' / ')) AS bronze_counter FROM s)
SELECT
    k.value AS kpi_value, k.coverage, k.suspect_share,
    b.period_start, b.object_dn, b.measurement, b.bin, b.value AS silver_value,
    b.derived, b.suspect, b.late, b.conflict, b.versions, b.dictionary_release,
    b.bronze_counter, p.value AS bronze_value, p.managed_element, p.parser_version, p.load_id,
    f.file_name, f.file_hash, f.arrival_time, f.size_bytes
FROM b CROSS JOIN k
LEFT JOIN lk.bronze.pm_values p ON p.ems = b.ems AND p.file_hash = b.file_hash
    AND p.object_dn = b.object_dn AND p.counter = b.bronze_counter
    AND p.period_start = b.period_start AND p.period_end = b.period_end
    AND p.period_start >= $lo AND p.period_start < $hi
LEFT JOIN lk.bronze.file_arrivals f ON f.file_hash = p.file_hash AND f.loaded
ORDER BY b.period_start, b.object_dn, b.measurement, b.bin, b.bronze_counter
```

Worked examples on the demo warehouse:

| KPI | Cell | Period | Value | Silver rows | Bronze rows | Files | Releases | Vendor counters | Derived | Suspect | Late | Conflict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| LTE_ERAB_DROP v1 | ENB0001_B3_1 | day 2026-02-10 | 0.4534 | 192 | 192 | 96 | HW-R1 | L.E-RAB.AbnormRel, L.E-RAB.SuccEst | 0 | 0 | 0 | 0 |
| GSM_TCH_BLOCK v1 | BTS0001_G900_1 | day 2026-02-10 | 1.1444 | 192 | 192 | 96 | HW-R1 | K3010A, K3011A | 0 | 0 | 0 | 0 |
| LTE_PRB_UTIL v1 | ENB0001_B3_1 | 15m 2026-02-10T04:00:00+00:00 | 23.0000 | 1 | 2 | 1 | HW-R1 | L.ChMeas.PRB.DL.Avail, L.ChMeas.PRB.DL.Used.Avg | 2 | 0 | 0 | 0 |

First row of each walk:

- LTE_ERAB_DROP v1: measurement ERAB.EstabInitSuccNbr.sum; silver_value 227.0; dictionary_release HW-R1; bronze_counter L.E-RAB.SuccEst; bronze_value 227.0; managed_element ManagedElement=ENB0001; file_name B20260210.0000+0700-0015+0700_EMS-HW-01.xml.gz; arrival_time 2026-02-09 17:17:16.270532+00:00
- GSM_TCH_BLOCK v1: measurement attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState; silver_value 47.0; dictionary_release HW-R1; bronze_counter K3010A; bronze_value 47.0; managed_element ManagedElement=BSC01; file_name B20260210.0000+0700-0015+0700_EMS-HW-01.xml.gz; arrival_time 2026-02-09 17:17:16.270532+00:00
- LTE_PRB_UTIL v1: measurement RRU.PrbTotDl; silver_value 23.0; dictionary_release HW-R1; bronze_counter L.ChMeas.PRB.DL.Avail; bronze_value 100.0; managed_element ManagedElement=ENB0001; file_name B20260210.1100+0700-1115+0700_EMS-HW-01.xml.gz; arrival_time 2026-02-10 04:20:30.334709+00:00

## Cost

| Step | Seconds |
|---|---|
| time travel staging | 74.5 |
| lineage walks | 9.4 |
