-- ============================================================
-- LexAI · F3 · Calculadoras: prescripción + intereses moratorios
-- Migration date: 2026-05-03
-- Idempotent · additive · NO DROP on existing objects
-- ============================================================

-- 1. legal_constants · centralizar valores (DTF, SMMLV, tasa máxima)
-- Permite actualizar trimestralmente sin redeploy.
create table if not exists legal_constants (
  key                varchar(80) primary key,
  value_numeric      numeric(18,6),
  value_text         text,
  value_jsonb        jsonb,
  unit               varchar(40),
  jurisdiccion       varchar(8) not null default 'CO',
  effective_from     date not null,
  effective_to       date,
  fuente             text,
  updated_at         timestamptz default now()
);
create index if not exists legal_constants_eff_idx
  on legal_constants (key, effective_from desc, jurisdiccion);

-- Seed CO 2026 — tasas verificables en SFC + Banrep + decretos
insert into legal_constants (key, value_numeric, unit, effective_from, fuente)
values
  ('co.dtf.anual',                0.1199, 'tasa_anual', '2026-01-01', 'Banrep certificación trimestral'),
  ('co.interes_corriente.anual',  0.1948, 'tasa_anual', '2026-01-01', 'SFC certificación trimestral'),
  ('co.interes_moratorio_max.anual', 0.2922, 'tasa_anual', '2026-01-01', 'Decreto 519/2007 Art. 5 (1.5x corriente)'),
  ('co.smmlv.mensual',            1823500, 'COP_mensual', '2026-01-01', 'Decreto 1572/2024'),
  ('co.aux_transporte.mensual',    200000, 'COP_mensual', '2026-01-01', 'Decreto 1572/2024'),
  ('co.tasa_legal_civil.anual',     0.06, 'tasa_anual', '1971-01-01', 'Código Civil Art. 1617 par. 2 — 6% legal supletivo')
on conflict (key) do nothing;

-- ============================================================
-- 2. calc_prescripciones · histórico de cálculos
-- ============================================================
create table if not exists calc_prescripciones (
  id                 uuid primary key default gen_random_uuid(),
  firm_id            uuid not null references firms(id) on delete cascade,
  matter_id          uuid references matters(id),
  user_id            uuid not null references users(id),
  case_label         text,                                -- "María Rodríguez · contrato 2018"
  tipo_accion        text not null,                       -- 'civil_ordinaria','civil_ejecutiva','laboral','comercial_ejecutiva','familiar_alimentos','penal'
  fecha_exigibilidad date not null,
  fecha_interrupcion date,                                 -- ej: notificación demanda (Art. 94 CGP)
  fecha_calculo      date not null default current_date,
  variables          jsonb not null,
  resultado          jsonb not null,                       -- line_items + fecha_prescripcion + dias_restantes
  fecha_prescripcion date not null,
  dias_restantes     integer not null,
  prescrita          boolean not null,
  formulas_version   text not null,                        -- 'co-prescripcion-v1'
  computed_at        timestamptz default now()
);
create index if not exists calc_prescripciones_firm_idx
  on calc_prescripciones (firm_id, computed_at desc);
create index if not exists calc_prescripciones_matter_idx
  on calc_prescripciones (matter_id) where matter_id is not null;

-- ============================================================
-- 3. calc_intereses · histórico de cálculos
-- ============================================================
create table if not exists calc_intereses (
  id                 uuid primary key default gen_random_uuid(),
  firm_id            uuid not null references firms(id) on delete cascade,
  matter_id          uuid references matters(id),
  user_id            uuid not null references users(id),
  case_label         text,
  tipo_interes       text not null,                        -- 'comercial_moratorio','civil_legal','laboral_cesantias','convencional'
  capital_cop        numeric(16,2) not null,
  fecha_inicio       date not null,
  fecha_fin          date not null,
  tasa_anual_aplicada numeric(8,5),                        -- almacenamos la tasa real usada
  base_calculo       text not null default '360',           -- '360' | '365'
  metodo             text not null default 'simple',        -- 'simple' | 'compuesto'
  variables          jsonb not null,
  resultado          jsonb not null,                        -- line_items con tramos por trimestre
  monto_total_cop    numeric(16,2) not null,
  formulas_version   text not null,                         -- 'co-intereses-v1'
  computed_at        timestamptz default now()
);
create index if not exists calc_intereses_firm_idx
  on calc_intereses (firm_id, computed_at desc);
create index if not exists calc_intereses_matter_idx
  on calc_intereses (matter_id) where matter_id is not null;

-- ============================================================
-- RLS · same pattern as liquidacion_calculations
-- ============================================================
alter table calc_prescripciones enable row level security;
alter table calc_intereses      enable row level security;
-- legal_constants es global (no firm_id); lectura pública para usuarios autenticados.
alter table legal_constants     enable row level security;

drop policy if exists calc_prescripciones_select on calc_prescripciones;
drop policy if exists calc_prescripciones_modify on calc_prescripciones;
create policy calc_prescripciones_select on calc_prescripciones for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy calc_prescripciones_modify on calc_prescripciones for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists calc_intereses_select on calc_intereses;
drop policy if exists calc_intereses_modify on calc_intereses;
create policy calc_intereses_select on calc_intereses for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy calc_intereses_modify on calc_intereses for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- legal_constants: cualquier authenticated lee, sólo service_role escribe.
drop policy if exists legal_constants_select on legal_constants;
drop policy if exists legal_constants_modify on legal_constants;
create policy legal_constants_select on legal_constants for select
  using (auth.role() in ('authenticated','service_role'));
create policy legal_constants_modify on legal_constants for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill (replicando patrón existente)
-- ============================================================
drop trigger if exists trg_calc_prescripciones_firm_id on calc_prescripciones;
create trigger trg_calc_prescripciones_firm_id
  before insert on calc_prescripciones
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_calc_intereses_firm_id on calc_intereses;
create trigger trg_calc_intereses_firm_id
  before insert on calc_intereses
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- Helper RPC · resolver constante vigente
-- ============================================================
create or replace function lexai_legal_constant(
  p_key text,
  p_at  date default current_date
) returns numeric
language sql
stable
as $$
  select value_numeric
  from legal_constants
  where key = p_key
    and effective_from <= p_at
    and (effective_to is null or effective_to > p_at)
  order by effective_from desc
  limit 1;
$$;

-- ============================================================
-- DONE
-- ============================================================
