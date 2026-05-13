-- ============================================================
-- LexAI · Sprint 10 · Trust Accounting + Conciliación Bancaria
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- Ley 1123/2007 (Código Disciplinario del Abogado) · obligación
-- de manejar fondos del cliente segregados con trazabilidad.
-- ============================================================

-- ------------------------------------------------------------
-- 1. trust_accounts · cuentas bancarias fiduciarias de la firma
-- ------------------------------------------------------------
create table if not exists trust_accounts (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  bank_name         text not null,
  account_number    text not null,                          -- últimos 4 ok para display
  account_type      text not null default 'corriente'       -- 'corriente','ahorros','escrow'
    check (account_type in ('corriente','ahorros','escrow')),
  currency          text not null default 'COP',
  is_trust          boolean not null default true,          -- fiduciaria si true
  active            boolean not null default true,
  notes             text,
  opening_balance_cop numeric(14,2) not null default 0,
  opening_date      date,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (firm_id, account_number)
);
create index if not exists trust_accounts_firm_idx
  on trust_accounts (firm_id, active) where active = true;

-- ------------------------------------------------------------
-- 2. trust_transactions · ledger de movimientos fiduciarios
-- ------------------------------------------------------------
create table if not exists trust_transactions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  trust_account_id  uuid not null references trust_accounts(id) on delete restrict,
  client_id         uuid references clients(id) on delete set null,
  matter_id         uuid references matters(id) on delete set null,
  kind              text not null check (kind in (
    'deposit',                  -- cliente deposita anticipo / consignación judicial
    'withdrawal',               -- pago a tercero (perito, arancel, notario)
    'fee_transfer',             -- transfer del trust al operating account (cobro honorarios)
    'refund',                   -- devolución al cliente
    'adjustment',               -- ajuste de error (poco común, auditable)
    'transfer_in',              -- entrada por transfer entre trusts
    'transfer_out'              -- salida por transfer entre trusts
  )),
  amount_cop        numeric(14,2) not null check (amount_cop > 0),
  direction         text not null check (direction in ('in','out')),
  occurred_on       date not null default current_date,
  description       text not null default '',
  reference         text,                                   -- número de cheque / transferencia bancaria
  payer_payee       text,                                   -- nombre de la contraparte (perito, etc.)
  related_invoice_id uuid references invoices(id) on delete set null,
  reconciled        boolean not null default false,
  reconciled_with   uuid,                                   -- bank_statement_line.id (FK creada después)
  reversal_of       uuid references trust_transactions(id) on delete set null,
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists tt_firm_account_idx
  on trust_transactions (firm_id, trust_account_id, occurred_on desc);
create index if not exists tt_client_idx
  on trust_transactions (client_id, occurred_on desc) where client_id is not null;
create index if not exists tt_matter_idx
  on trust_transactions (matter_id, occurred_on desc) where matter_id is not null;
create index if not exists tt_unreconciled_idx
  on trust_transactions (firm_id, trust_account_id, occurred_on)
  where reconciled = false;

-- ------------------------------------------------------------
-- 3. bank_statements · extractos bancarios importados
-- ------------------------------------------------------------
create table if not exists bank_statements (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  trust_account_id  uuid not null references trust_accounts(id) on delete cascade,
  period_start      date not null,
  period_end        date not null,
  opening_balance_cop numeric(14,2) not null default 0,
  closing_balance_cop numeric(14,2) not null default 0,
  source_filename   text,
  source_format     text default 'csv',                     -- 'csv','ofx','manual'
  imported_by       uuid references users(id) on delete set null,
  imported_at       timestamptz default now(),
  unique (trust_account_id, period_start, period_end)
);
create index if not exists bank_stmt_firm_idx
  on bank_statements (firm_id, imported_at desc);

-- ------------------------------------------------------------
-- 4. bank_statement_lines · líneas individuales del extracto
-- ------------------------------------------------------------
create table if not exists bank_statement_lines (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  bank_statement_id uuid not null references bank_statements(id) on delete cascade,
  occurred_on       date not null,
  amount_cop        numeric(14,2) not null,                  -- signo: + entrada, - salida
  description       text not null default '',
  reference         text,
  balance_after_cop numeric(14,2),
  matched_transaction_id uuid references trust_transactions(id) on delete set null,
  match_confidence  numeric(3,2),                            -- 0.0-1.0 cuando auto-match
  match_method      text,                                    -- 'auto_exact','auto_fuzzy','manual'
  created_at        timestamptz default now()
);
create index if not exists bsl_stmt_idx
  on bank_statement_lines (bank_statement_id, occurred_on);
create index if not exists bsl_unmatched_idx
  on bank_statement_lines (firm_id, bank_statement_id)
  where matched_transaction_id is null;

-- ------------------------------------------------------------
-- FK cíclica: trust_transactions.reconciled_with → bank_statement_lines.id
-- ------------------------------------------------------------
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'tt_reconciled_with_fk') then
    alter table trust_transactions
      add constraint tt_reconciled_with_fk
      foreign key (reconciled_with) references bank_statement_lines(id) on delete set null;
  end if;
end $$;

-- ============================================================
-- RLS
-- ============================================================
alter table trust_accounts        enable row level security;
alter table trust_transactions    enable row level security;
alter table bank_statements       enable row level security;
alter table bank_statement_lines  enable row level security;

drop policy if exists ta_select on trust_accounts;
drop policy if exists ta_modify on trust_accounts;
create policy ta_select on trust_accounts for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ta_modify on trust_accounts for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists tt_select on trust_transactions;
drop policy if exists tt_modify on trust_transactions;
create policy tt_select on trust_transactions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy tt_modify on trust_transactions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists bs_select on bank_statements;
drop policy if exists bs_modify on bank_statements;
create policy bs_select on bank_statements for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy bs_modify on bank_statements for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists bsl_select on bank_statement_lines;
drop policy if exists bsl_modify on bank_statement_lines;
create policy bsl_select on bank_statement_lines for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy bsl_modify on bank_statement_lines for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_ta_firm_id on trust_accounts;
create trigger trg_ta_firm_id before insert on trust_accounts
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_ta_updated_at on trust_accounts;
create trigger trg_ta_updated_at before update on trust_accounts
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_tt_firm_id on trust_transactions;
create trigger trg_tt_firm_id before insert on trust_transactions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_bs_firm_id on bank_statements;
create trigger trg_bs_firm_id before insert on bank_statements
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_bsl_firm_id on bank_statement_lines;
create trigger trg_bsl_firm_id before insert on bank_statement_lines
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPCs · balances y reportes
-- ============================================================

-- Balance vigente de una cuenta fiduciaria
create or replace function lexai_trust_account_balance(p_account_id uuid)
returns numeric language sql stable as $$
  with a as (
    select opening_balance_cop from trust_accounts where id = p_account_id
  ),
  ins as (
    select coalesce(sum(amount_cop), 0) as v
      from trust_transactions
     where trust_account_id = p_account_id and direction = 'in'
  ),
  outs as (
    select coalesce(sum(amount_cop), 0) as v
      from trust_transactions
     where trust_account_id = p_account_id and direction = 'out'
  )
  select coalesce((select opening_balance_cop from a), 0)
       + coalesce((select v from ins), 0)
       - coalesce((select v from outs), 0);
$$;

-- Balance por matter (suma sobre todas las cuentas)
create or replace function lexai_trust_matter_balance(p_matter_id uuid)
returns numeric language sql stable as $$
  with ins as (
    select coalesce(sum(amount_cop), 0) as v
      from trust_transactions
     where matter_id = p_matter_id and direction = 'in'
  ),
  outs as (
    select coalesce(sum(amount_cop), 0) as v
      from trust_transactions
     where matter_id = p_matter_id and direction = 'out'
  )
  select coalesce((select v from ins), 0) - coalesce((select v from outs), 0);
$$;

-- Resumen general para dashboard /trust
create or replace function lexai_trust_summary(p_firm_id uuid default null)
returns jsonb language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
       accts as (
         select count(*)::int as n, coalesce(sum(opening_balance_cop), 0) as opening
           from trust_accounts where firm_id = (select id from f) and active = true
       ),
       movements as (
         select
           coalesce(sum(case when direction = 'in' then amount_cop else 0 end), 0) as total_in,
           coalesce(sum(case when direction = 'out' then amount_cop else 0 end), 0) as total_out
           from trust_transactions where firm_id = (select id from f)
       ),
       unreconciled as (
         select count(*)::int as n
           from trust_transactions
          where firm_id = (select id from f) and reconciled = false
       ),
       by_matter as (
         select coalesce(jsonb_object_agg(matter_id::text, balance), '{}'::jsonb) as obj
         from (
           select matter_id,
             sum(case when direction = 'in' then amount_cop else 0 end)
             - sum(case when direction = 'out' then amount_cop else 0 end) as balance
             from trust_transactions
            where firm_id = (select id from f) and matter_id is not null
            group by matter_id
            having sum(case when direction = 'in' then amount_cop else 0 end)
                 - sum(case when direction = 'out' then amount_cop else 0 end) <> 0
         ) x
       )
  select jsonb_build_object(
    'accounts_count', (select n from accts),
    'opening_total_cop', (select opening from accts),
    'total_in_cop', (select total_in from movements),
    'total_out_cop', (select total_out from movements),
    'current_balance_cop',
      (select opening from accts)
      + (select total_in from movements)
      - (select total_out from movements),
    'unreconciled_count', (select n from unreconciled),
    'by_matter_balance', (select obj from by_matter)
  );
$$;

-- Detalle por matter — ledger ordenado
create or replace function lexai_trust_matter_ledger(p_matter_id uuid)
returns table (
  id uuid,
  occurred_on date,
  kind text,
  direction text,
  amount_cop numeric,
  description text,
  reference text,
  payer_payee text,
  reconciled boolean,
  running_balance numeric
) language sql stable as $$
  with rows as (
    select id, occurred_on, kind, direction, amount_cop,
           description, reference, payer_payee, reconciled, created_at,
           case direction when 'in' then amount_cop else -amount_cop end as signed
      from trust_transactions
     where matter_id = p_matter_id
     order by occurred_on, created_at
  )
  select id, occurred_on, kind, direction, amount_cop,
         description, reference, payer_payee, reconciled,
         sum(signed) over (order by occurred_on, created_at) as running_balance
    from rows;
$$;
