{#- The CQI distribution per cell and hour (60-minute blocks, rules P1 and
    P4): samples and the CQI-weighted sum (TS 36.213 CQI index 0 to 15). -#}
{{ config(
    unique_key=['cell_name', 'period_start'],
    incremental_predicates=kpi_predicates('period_start', 'day_start', 'day_end', 'ts', false),
    post_hook={'sql': "{{ partition_by('day(period_start)') }}", 'transaction': false},
) }}

with r as (
    {{ silver_rows(60, ['CARR.WBCQIDist.Bin']) }}
)
select
    c.cell_name,
    r.period_start,
    any_value(r.vendor) as vendor,
    sum(r.value * r.bin) as cqi_weighted,
    sum(r.value) as cqi_samples,
    count(r.value) > 0 as reported,
    bool_or(r.suspect) as suspect
from r
join {{ ref('cells') }} c on c.ems = r.ems and c.dn = r.cell_dn and c.technology = 'LTE'
group by c.cell_name, r.period_start
