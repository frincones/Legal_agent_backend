-- ============================================================
-- LexAI · Sprint 8 · Horas facturables + Gastos + Facturación + Portal cliente
-- Migration date: 2026-05-10
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. matter_hourly_rates · tarifa por hora por usuario en cada matter
-- ------------------------------------------------------------
create table if not exists matter_hourly_rates (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  rate_cop          numeric(12,2) not null check (rate_cop >= 0),
  effective_from    date not null default current_date,
  effective_to      date,
  created_at        timestamptz default now()
);
create index if not exists hourly_rates_matter_idx
  on matter_hourly_rates (matter_id, user_id, effective_from desc);

-- ------------------------------------------------------------
-- 2. time_entries · ledger de horas trabajadas
-- ------------------------------------------------------------
create table if not exists time_entries (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  started_at        timestamptz not null default now(),
  ended_at          timestamptz,                             -- null = timer activo
  duration_min      int generated always as (
    case when ended_at is null then 0
         else greatest(0, extract(epoch from (ended_at - started_at))::int / 60)
    end
  ) stored,
  billable          boolean not null default true,
  rate_cop          numeric(12,2),                           -- snapshot al cerrar
  description       text not null default '',
  source            text not null default 'manual'           -- 'manual'|'timer'|'voice'
    check (source in ('manual','timer','voice')),
  invoice_line_id   uuid,                                    -- set cuando se factura
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists time_entries_matter_idx
  on time_entries (matter_id, started_at desc);
create index if not exists time_entries_user_idx
  on time_entries (user_id, started_at desc);
create index if not exists time_entries_unbilled_idx
  on time_entries (firm_id, matter_id) where invoice_line_id is null and billable = true;
create index if not exists time_entries_running_idx
  on time_entries (user_id, firm_id) where ended_at is null;

-- ------------------------------------------------------------
-- 3. expenses · gastos del caso (reembolsables o no)
-- ------------------------------------------------------------
create table if not exists expenses (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid not null references matters(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  kind              text not null,                           -- 'desplazamiento','copias','aranceles','peritaje','otro'
  amount_cop        numeric(12,2) not null check (amount_cop >= 0),
  occurred_on       date not null default current_date,
  description       text not null default '',
  billable          boolean not null default true,
  receipt_path      text,                                    -- supabase storage path
  invoice_line_id   uuid,
  created_at        timestamptz default now()
);
create index if not exists expenses_matter_idx
  on expenses (matter_id, occurred_on desc);
create index if not exists expenses_unbilled_idx
  on expenses (firm_id, matter_id) where invoice_line_id is null and billable = true;

-- ------------------------------------------------------------
-- 4. invoices · facturas a clientes
-- ------------------------------------------------------------
create table if not exists invoices (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  client_id         uuid not null references clients(id),
  matter_id         uuid references matters(id) on delete set null,
  number            text not null,                           -- '2026-0001' (sequence per firm)
  period_start      date,
  period_end        date,
  subtotal_cop      numeric(14,2) not null default 0,
  tax_pct           numeric(5,2) not null default 19.0,      -- IVA Colombia
  tax_cop           numeric(14,2) not null default 0,
  retencion_cop     numeric(14,2) not null default 0,        -- retención fuente
  total_cop         numeric(14,2) not null default 0,
  currency          text not null default 'COP',
  status            text not null default 'draft'
    check (status in ('draft','sent','paid','partially_paid','void','overdue')),
  due_date          date,
  sent_at           timestamptz,
  paid_at           timestamptz,
  paid_amount_cop   numeric(14,2) not null default 0,
  notes             text,
  pdf_url           text,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (firm_id, number)
);
create index if not exists invoices_firm_status_idx
  on invoices (firm_id, status, due_date);
create index if not exists invoices_client_idx
  on invoices (client_id, created_at desc);

-- ------------------------------------------------------------
-- 5. invoice_lines · detalle de cada factura
-- ------------------------------------------------------------
create table if not exists invoice_lines (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  invoice_id        uuid not null references invoices(id) on delete cascade,
  kind              text not null check (kind in ('time','expense','fixed','discount')),
  description       text not null,
  qty               numeric(10,2) not null default 1,
  unit_price_cop    numeric(12,2) not null default 0,
  total_cop         numeric(14,2) not null default 0,
  time_entry_id     uuid references time_entries(id) on delete set null,
  expense_id        uuid references expenses(id) on delete set null,
  position          int not null default 0,
  created_at        timestamptz default now()
);
create index if not exists invoice_lines_invoice_idx
  on invoice_lines (invoice_id, position);

-- Foreign key cíclica (time_entries.invoice_line_id) — la creamos después.
do $$ begin
  if not exists (
    select 1 from pg_constraint where conname = 'time_entries_invoice_line_fk'
  ) then
    alter table time_entries
      add constraint time_entries_invoice_line_fk
      foreign key (invoice_line_id) references invoice_lines(id) on delete set null;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'expenses_invoice_line_fk'
  ) then
    alter table expenses
      add constraint expenses_invoice_line_fk
      foreign key (invoice_line_id) references invoice_lines(id) on delete set null;
  end if;
end $$;

-- ------------------------------------------------------------
-- 6. firm_invoice_counters · numeración por firma
-- ------------------------------------------------------------
create table if not exists firm_invoice_counters (
  firm_id           uuid primary key references firms(id) on delete cascade,
  prefix            text not null default to_char(current_date, 'YYYY'),
  next_number       int not null default 1,
  updated_at        timestamptz default now()
);

-- Función para obtener el siguiente número de factura
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
     set next_number = next_number + 1,
         updated_at = now(),
         prefix = case when prefix <> to_char(current_date, 'YYYY')
                       then to_char(current_date, 'YYYY')
                       else prefix end,
         next_number = case when prefix <> to_char(current_date, 'YYYY')
                            then 2 else next_number + 1 end
   where firm_id = p_firm_id
   returning prefix, next_number - 1 into v_prefix, v_num;
  return v_prefix || '-' || lpad(v_num::text, 4, '0');
end;
$$;

-- ------------------------------------------------------------
-- 7. client_portal_tokens · magic-link auth para clientes
-- ------------------------------------------------------------
create table if not exists client_portal_tokens (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  client_id         uuid not null references clients(id) on delete cascade,
  token             text not null unique,                    -- random 32-char hex
  scope             text[] default array['matters','invoices','documents']::text[],
  expires_at        timestamptz not null,
  last_used_at      timestamptz,
  use_count         int not null default 0,
  revoked_at        timestamptz,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists cp_tokens_client_idx
  on client_portal_tokens (client_id) where revoked_at is null;

-- ============================================================
-- RLS
-- ============================================================
alter table matter_hourly_rates    enable row level security;
alter table time_entries           enable row level security;
alter table expenses               enable row level security;
alter table invoices               enable row level security;
alter table invoice_lines          enable row level security;
alter table client_portal_tokens   enable row level security;
alter table firm_invoice_counters  enable row level security;

drop policy if exists hr_select on matter_hourly_rates;
drop policy if exists hr_modify on matter_hourly_rates;
create policy hr_select on matter_hourly_rates for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy hr_modify on matter_hourly_rates for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists te_select on time_entries;
drop policy if exists te_modify on time_entries;
create policy te_select on time_entries for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy te_modify on time_entries for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ex_select on expenses;
drop policy if exists ex_modify on expenses;
create policy ex_select on expenses for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ex_modify on expenses for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists inv_select on invoices;
drop policy if exists inv_modify on invoices;
create policy inv_select on invoices for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy inv_modify on invoices for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists invl_select on invoice_lines;
drop policy if exists invl_modify on invoice_lines;
create policy invl_select on invoice_lines for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy invl_modify on invoice_lines for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists cpt_select on client_portal_tokens;
drop policy if exists cpt_modify on client_portal_tokens;
create policy cpt_select on client_portal_tokens for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cpt_modify on client_portal_tokens for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists fic_modify on firm_invoice_counters;
create policy fic_modify on firm_invoice_counters for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_te_firm_id on time_entries;
create trigger trg_te_firm_id before insert on time_entries
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_te_updated_at on time_entries;
create trigger trg_te_updated_at before update on time_entries
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_ex_firm_id on expenses;
create trigger trg_ex_firm_id before insert on expenses
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_inv_firm_id on invoices;
create trigger trg_inv_firm_id before insert on invoices
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_inv_updated_at on invoices;
create trigger trg_inv_updated_at before update on invoices
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_invl_firm_id on invoice_lines;
create trigger trg_invl_firm_id before insert on invoice_lines
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_cpt_firm_id on client_portal_tokens;
create trigger trg_cpt_firm_id before insert on client_portal_tokens
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_hr_firm_id on matter_hourly_rates;
create trigger trg_hr_firm_id before insert on matter_hourly_rates
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPC · summary previo a facturar
-- ============================================================

create or replace function lexai_billable_summary(
  p_firm_id   uuid default null,
  p_matter_id uuid default null,
  p_since     date default null,
  p_until     date default null
) returns jsonb
language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
       te as (
         select t.id, t.matter_id, t.user_id, t.duration_min,
                coalesce(t.rate_cop,
                  (select rate_cop from matter_hourly_rates h
                    where h.matter_id = t.matter_id and (h.user_id = t.user_id or h.user_id is null)
                      and (h.effective_to is null or h.effective_to >= t.started_at::date)
                    order by h.effective_from desc limit 1),
                  0
                ) as rate_cop,
                t.description
           from time_entries t
          where t.firm_id = (select id from f)
            and t.invoice_line_id is null
            and t.ended_at is not null
            and t.billable = true
            and (p_matter_id is null or t.matter_id = p_matter_id)
            and (p_since is null or t.started_at::date >= p_since)
            and (p_until is null or t.started_at::date <= p_until)
       ),
       ex as (
         select e.id, e.matter_id, e.amount_cop, e.kind, e.description, e.occurred_on
           from expenses e
          where e.firm_id = (select id from f)
            and e.invoice_line_id is null
            and e.billable = true
            and (p_matter_id is null or e.matter_id = p_matter_id)
            and (p_since is null or e.occurred_on >= p_since)
            and (p_until is null or e.occurred_on <= p_until)
       )
  select jsonb_build_object(
    'time_minutes',  coalesce((select sum(duration_min) from te), 0),
    'time_amount',   coalesce((select sum(duration_min * rate_cop / 60.0) from te), 0),
    'time_entries',  coalesce((select count(*) from te), 0),
    'expense_amount',coalesce((select sum(amount_cop) from ex), 0),
    'expense_count', coalesce((select count(*) from ex), 0),
    'subtotal',      coalesce((select sum(duration_min * rate_cop / 60.0) from te), 0)
                     + coalesce((select sum(amount_cop) from ex), 0),
    'period_start',  p_since,
    'period_end',    p_until,
    'matter_id',     p_matter_id
  );
$$;
