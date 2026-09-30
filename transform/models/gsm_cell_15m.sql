{#- GSM counters per cell and 15-minute period under TS 52.402 names from
    silver. tch_req is the vendor-style sum of TCH seizure attempts and
    attempts that met all TCHs busy; ho_unsucc the sum of unsuccessful
    handovers with reconnection and with loss of connection. -#}
{%- set columns = [
    ('tch_req', 'attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState'),
    ('tch_blocked', 'attTCHSeizuresMeetingTCHBlockedState'),
    ('tch_succ', 'succTCHSeizures'),
    ('ia_att', 'attImmediateAssingProcs'), ('ia_succ', 'succImmediateAssingProcs'),
    ('sdcch_blocked', 'attSDCCHSeizuresMeetingSDCCHBlockedState'),
    ('sdcch_lost', 'nbrOfLostRadioLinksSDCCH'), ('tch_lost', 'nbrOfLostRadioLinksTCH'),
    ('ho_att', 'attOutgoingInternalInterCellHDOs'),
    ('ho_succ', 'succOutgoingInternalInterCellHDOs'),
    ('ho_unsucc', 'unsuccHDOsWithReconnection + unsuccHDOsWithLossOfConnection'),
    ('ho_in', 'succIncomingInternalInterCellHDOs'),
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
join {{ ref('cells') }} c on c.ems = r.ems and c.dn = r.cell_dn and c.technology = 'GSM'
group by c.cell_name, r.period_start
