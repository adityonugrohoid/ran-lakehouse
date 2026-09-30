{#- LTE counters per cell and 15-minute period, under 3GPP names (TS 32.425)
    from silver. Handover counters are per neighbour relation in silver and
    summed here to their source cell. reported: the cell has any value in
    the period; suspect: any of its values is flagged (rule D4). -#}
{%- set columns = [
    ('rrc_att', 'RRC.ConnEstabAtt.sum'), ('rrc_succ', 'RRC.ConnEstabSucc.sum'),
    ('s1_att', 'S1SIG.ConnEstabAtt'), ('s1_succ', 'S1SIG.ConnEstabSucc'),
    ('erab_att', 'ERAB.EstabInitAttNbr.sum'), ('erab_succ', 'ERAB.EstabInitSuccNbr.sum'),
    ('erab_rel', 'ERAB.RelActNbr.sum'), ('session_s', 'ERAB.SessionTimeUE'),
    ('ip_vol_kbit', 'DRB.IPVolDl.sum'), ('ip_time_ms', 'DRB.IPTimeDl.sum'),
    ('prb_pct', 'RRU.PrbTotDl'), ('unavail_s', 'RRU.CellUnavailableTime.sum'),
    ('ho_att', 'HO.OutAttTarget.sum'), ('ho_succ', 'HO.OutSuccTarget.sum'),
] -%}
{{ config(
    unique_key=['cell_name', 'period_start'],
    incremental_predicates=kpi_predicates('period_start', 'day_start', 'day_end', 'ts', false),
    post_hook={'sql': "{{ partition_by('day(period_start)') }}", 'transaction': false},
) }}

with r as (
    {{ silver_rows(15, columns | map(attribute=1) | list) }}
)
select
    c.cell_name,
    r.period_start,
    any_value(r.vendor) as vendor,
    {{ pivot(columns) }}
    count(r.value) > 0 as reported,
    bool_or(r.suspect) as suspect
from r
join {{ ref('cells') }} c on c.ems = r.ems and c.dn = r.cell_dn and c.technology = 'LTE'
group by c.cell_name, r.period_start
