-- Fix: lexai_next_invoice_number() had duplicate assignment to next_number
-- in a single UPDATE (lines 174 and 179 of sprint8 migration) which Postgres
-- rejects with "multiple assignments to same column".
-- Merge both into a single CASE expression.

begin;

create or replace function lexai_next_invoice_number(p_firm_id uuid)
returns text
language plpgsql
as $$
declare
  v_prefix text;
  v_num int;
begin
  insert into firm_invoice_counters (firm_id, prefix, next_number)
    values (p_firm_id, to_char(current_date, 'YYYY'), 1)
    on conflict (firm_id) do nothing;
  update firm_invoice_counters
     set next_number = case
                         when prefix <> to_char(current_date, 'YYYY') then 2
                         else next_number + 1
                       end,
         prefix = case
                    when prefix <> to_char(current_date, 'YYYY')
                      then to_char(current_date, 'YYYY')
                    else prefix
                  end,
         updated_at = now()
   where firm_id = p_firm_id
   returning prefix, next_number - 1 into v_prefix, v_num;
  return v_prefix || '-' || lpad(v_num::text, 4, '0');
end;
$$;

commit;
