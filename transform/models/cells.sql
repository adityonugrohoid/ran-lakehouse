{#- Cells of both EMS from their CM snapshots (rule C1): the DN each EMS
    uses, the cell name (userLabel) that joins the two vendors, and for LTE
    cells the downlink resource blocks (N_RB). -#}
{{ config(unique_key=['ems', 'dn']) }}

select distinct
    ems,
    json_extract_string(record, '$.dn') as dn,
    json_extract_string(record, '$.attributes.userLabel') as cell_name,
    case
        when json_extract_string(record, '$.objectClass') like 'EUtranCell%' then 'LTE'
        else 'GSM'
    end as technology,
    {#- Downlink resource blocks from the channel bandwidth, TS 36.101
        Table 5.6-1; weights PRB utilization across cells. -#}
    case cast(json_extract_string(record, '$.attributes.bandwidthMhz') as double)
        when 1.4 then 6 when 3 then 15 when 5 then 25 when 10 then 50
        when 15 then 75 when 20 then 100
    end as n_rb
from {{ source('bronze', 'cm_records') }}
where kind = 'CM'
    and json_extract_string(record, '$.objectClass') in
        ('EUtranCellFDD', 'EUtranCellTDD', 'GsmCell')
    and cast(json_extract_string(record, '$.snapshotTime') as timestamptz) < {{ ts('day_end') }}
