{#- Cells of both EMS from their CM snapshots (rule C1): the DN each EMS
    uses, and the cell name (userLabel) that joins the two vendors. -#}
{{ config(unique_key=['ems', 'dn']) }}

select distinct
    ems,
    json_extract_string(record, '$.dn') as dn,
    json_extract_string(record, '$.attributes.userLabel') as cell_name,
    case
        when json_extract_string(record, '$.objectClass') like 'EUtranCell%' then 'LTE'
        else 'GSM'
    end as technology
from {{ source('bronze', 'cm_records') }}
where kind = 'CM'
    and json_extract_string(record, '$.objectClass') in
        ('EUtranCellFDD', 'EUtranCellTDD', 'GsmCell')
    and cast(json_extract_string(record, '$.snapshotTime') as timestamptz) < {{ ts('day_end') }}
