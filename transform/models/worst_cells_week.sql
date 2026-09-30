{#- Weekly worst cells per KPI (rule L5). A day is judged when its coverage
    is at least min_coverage; it breaches when its value is worse than the
    KPI's breach threshold (kpi_catalog). A cell is persistent in a WIB week
    when it breaches on at least persistence_n of the week's
    persistence_m days; persistent cells are ranked by breach days, then by
    how far the week's value is past the threshold. Recomputed as the week's
    days arrive. -#}
{{ config(
    unique_key=['week_start', 'kpi_id', 'formula_version', 'cell_name'],
    incremental_predicates=kpi_predicates('week_start', 'week_first', 'week_last', 'date'),
) }}

with catalog as (
    select kpi_id, formula_version, better, breach_threshold
    from {{ source('gold_static', 'kpi_catalog') }}
    where breach_threshold is not null
    {%- if var('only_kpi') %}
        and kpi_id = '{{ var("only_kpi") }}'
    {%- endif %}
),
days as (
    select cell_name, day, kpi_id, formula_version, value, coverage from {{ ref('lte_kpi_day') }}
    union all
    select cell_name, day, kpi_id, formula_version, value, coverage from {{ ref('gsm_kpi_day') }}
),
weeks as (
    select cell_name, week_start, kpi_id, formula_version, value from {{ ref('lte_kpi_week') }}
    union all
    select cell_name, week_start, kpi_id, formula_version, value from {{ ref('gsm_kpi_week') }}
),
judged as (
    select
        d.cell_name,
        cast(date_trunc('week', d.day) as date) as week_start,
        d.kpi_id, d.formula_version,
        case when c.better = 'higher' then d.value < c.breach_threshold
             else d.value > c.breach_threshold end as breach
    from days d
    join catalog c using (kpi_id, formula_version)
    where d.day >= {{ dt('week_first') }} and d.day < {{ dt('week_last') }} + 7
        and d.coverage >= {{ var('min_coverage') }} and d.value is not null
),
counted as (
    select cell_name, week_start, kpi_id, formula_version,
        count(*) as days_judged,
        count(*) filter (where breach) as breach_days
    from judged
    group by all
),
persistent as (
    select
        n.*, w.value as week_value, c.breach_threshold, c.better,
        case when c.better = 'higher' then c.breach_threshold - w.value
             else w.value - c.breach_threshold end as margin
    from counted n
    join catalog c using (kpi_id, formula_version)
    left join weeks w using (cell_name, week_start, kpi_id, formula_version)
    where n.breach_days >= {{ var('persistence_n') }}
)
select
    week_start, kpi_id, formula_version, cell_name, days_judged, breach_days,
    {{ var('persistence_n') }} as persistence_n,
    {{ var('persistence_m') }} as persistence_m,
    week_value, breach_threshold, better,
    row_number() over (
        partition by week_start, kpi_id, formula_version
        order by breach_days desc, margin desc nulls last, cell_name
    ) as rank
from persistent
