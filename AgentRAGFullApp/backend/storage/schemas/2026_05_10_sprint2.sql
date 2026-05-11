-- ============================================================
-- Sprint 2 · Casos avanzados + Tabs nuevos
-- ============================================================
-- Date: 2026-05-09
-- Adds:
--   1. matters.instance       (administrativa/primera/apelacion/casacion/firme)
--   2. matters.proceso_tipo   (texto libre · ej. "tutela", "ordinaria")
--   3. matters.current_term_due_at (timestamptz · próximo plazo crítico)
--   4. case_risks             (tabla M32 · riesgos detectados por caso)
--
-- Idempotent.
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. matters.instance + proceso_tipo + current_term_due_at
-- ──────────────────────────────────────────────────────────────
alter table matters
  add column if not exists instance text
  check (instance in ('inicial','administrativa','primera','apelacion','casacion','firme'))
  default 'inicial';

alter table matters
  add column if not exists proceso_tipo text;

alter table matters
  add column if not exists current_term_due_at timestamptz;

-- Backfill: existing matters get 'inicial' explicitly (the default
-- only applies on new rows, not existing ones).
update matters set instance = 'inicial' where instance is null;

-- ──────────────────────────────────────────────────────────────
-- 2. case_risks (M32 placeholder · llenado por agentes/workers)
-- ──────────────────────────────────────────────────────────────
create table if not exists case_risks (
  id           uuid primary key default gen_random_uuid(),
  firm_id      uuid not null references firms(id) on delete cascade,
  matter_id    uuid not null references matters(id) on delete cascade,
  type         text not null check (type in (
    'vencimiento','jurisprudencia_adversa','cambio_normativo',
    'parte_poderosa','citas_debilitadas','documento_faltante',
    'inconsistencia','plazo_corto','otro'
  )),
  severity     int not null default 5 check (severity between 1 and 10),
  title        text not null,
  description  text,
  evidence_url text,
  mitigation   text,
  detected_by  text default 'agent' check (detected_by in ('agent','user','worker','manual')),
  detected_at  timestamptz default now(),
  resolved_at  timestamptz,
  resolved_by  uuid references users(id),
  metadata     jsonb default '{}'::jsonb
);

create index if not exists case_risks_matter_idx on case_risks (matter_id, detected_at desc);
create index if not exists case_risks_firm_open_idx on case_risks (firm_id) where resolved_at is null;

alter table case_risks enable row level security;

drop policy if exists case_risks_select on case_risks;
create policy case_risks_select on case_risks
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists case_risks_modify on case_risks;
create policy case_risks_modify on case_risks
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

commit;

-- ──────────────────────────────────────────────────────────────
-- Verification:
--   select column_name from information_schema.columns
--    where table_name='matters' and column_name in ('instance','proceso_tipo','current_term_due_at');
--   select rowsecurity from pg_tables where tablename='case_risks';
-- ──────────────────────────────────────────────────────────────
