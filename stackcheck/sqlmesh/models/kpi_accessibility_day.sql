-- Rule D6: the formula line below is edited by ran_lakehouse.stackcheck to
-- change the KPI formula; SQLMesh sees a breaking change and backfills.
MODEL (
  name lk.sqlmesh_gold.kpi_accessibility_day,
  kind INCREMENTAL_BY_TIME_RANGE (time_column day),
  grain (cell_id, day),
  end '2026-01-18'
);

SELECT
  cell_id,
  CAST(period_start AS DATE) AS day,
  1 AS formula_version, sum(rrc_succ) / sum(rrc_att) AS value -- formula
FROM lk.ran.counters
WHERE CAST(period_start AS DATE) BETWEEN @start_ds AND @end_ds
GROUP BY cell_id, CAST(period_start AS DATE)
