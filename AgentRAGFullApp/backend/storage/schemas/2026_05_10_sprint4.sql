-- ============================================================
-- Sprint 4 · Templates + Word + Calculadoras
-- ============================================================
-- Date: 2026-05-10
-- Adds:
--   1. user_templates             (private firm/user docx templates)
--   2. legal_constants seed       (pension thresholds + judicial vacancia 2026 CO)
--
-- Idempotent.
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. user_templates (M15 · 3 niveles: global / firma / usuario)
-- ──────────────────────────────────────────────────────────────
create table if not exists user_templates (
  id            uuid primary key default gen_random_uuid(),
  firm_id       uuid not null references firms(id) on delete cascade,
  -- owner_id null = template compartido por toda la firma
  owner_id      uuid references users(id) on delete cascade,
  name          text not null,
  doc_type      text not null check (doc_type in (
    'tutela','contestacion','demanda_laboral','demanda_civil',
    'derecho_peticion','recurso_apelacion','casacion',
    'recurso_reposicion','dictamen','memorial','contrato','otro'
  )),
  jurisdiction  text default 'CO',
  -- target_court: ej. "juzgado_civil_circuito" · usado en Sprint 11
  target_court  text,
  content_md    text not null,
  -- variables detectadas en content_md ({{nombre}}, {{nit}}, etc.)
  variables     jsonb default '[]'::jsonb,
  source_docx_path text,
  is_default_for_type boolean default false,
  metadata      jsonb default '{}'::jsonb,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists user_templates_firm_idx
  on user_templates (firm_id, doc_type);
create index if not exists user_templates_owner_idx
  on user_templates (owner_id) where owner_id is not null;

-- Solo UN default por (firm, doc_type, target_court).
create unique index if not exists user_templates_unique_default
  on user_templates (firm_id, doc_type, coalesce(target_court, ''))
  where is_default_for_type = true;

alter table user_templates enable row level security;

drop policy if exists user_templates_select on user_templates;
create policy user_templates_select on user_templates
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists user_templates_modify on user_templates;
create policy user_templates_modify on user_templates
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

-- Trigger updated_at if helper exists (created in earlier migrations).
do $$
begin
  if exists (select 1 from pg_proc where proname = 'tg_set_updated_at') then
    drop trigger if exists user_templates_set_updated_at on user_templates;
    create trigger user_templates_set_updated_at
      before update on user_templates
      for each row execute function tg_set_updated_at();
  end if;
end $$;

-- ──────────────────────────────────────────────────────────────
-- 2. legal_constants seed for pension and plazos (CO, 2026)
-- ──────────────────────────────────────────────────────────────

-- SMLMV 2026 (decreto 2295/2025 · valor referencial)
insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('smmlv', 1620000, 'COP', 'CO', '2026-01-01', 'Decreto 2295/2025')
on conflict do nothing;

-- Pensión vejez · semanas mínimas (Ley 100/93 art. 33)
insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('pension_vejez_semanas_minimas', 1300, 'semanas', 'CO', '2015-01-01', 'Ley 797/2003 art.9')
on conflict do nothing;

-- Edad mínima vejez por género
insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('pension_vejez_edad_mujer', 57, 'años', 'CO', '2014-01-01', 'Ley 797/2003 art.9')
on conflict do nothing;

insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('pension_vejez_edad_hombre', 62, 'años', 'CO', '2014-01-01', 'Ley 797/2003 art.9')
on conflict do nothing;

-- Pensión invalidez · semanas en últimos 3 años
insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('pension_invalidez_semanas_3a', 50, 'semanas', 'CO', '2003-12-29', 'Ley 860/2003 art.1')
on conflict do nothing;

-- Pensión sobrevivencia · semanas en últimos 3 años
insert into legal_constants (key, value_numeric, unit, jurisdiccion, effective_from, fuente)
values ('pension_sobrevivencia_semanas_3a', 50, 'semanas', 'CO', '2003-12-29', 'Ley 797/2003 art.12')
on conflict do nothing;

-- Vacancia judicial 2026 CO · array de rangos [start, end]
-- (Acuerdo Consejo Superior de la Judicatura)
insert into legal_constants (key, value_jsonb, unit, jurisdiccion, effective_from, fuente)
values (
  'vacancia_judicial_2026',
  jsonb_build_array(
    jsonb_build_object('label','Semana Santa','from','2026-03-30','to','2026-04-03'),
    jsonb_build_object('label','Vacancia de mitad de año','from','2026-06-22','to','2026-07-10'),
    jsonb_build_object('label','Vacancia de fin de año','from','2026-12-19','to','2027-01-11')
  ),
  'rangos',
  'CO',
  '2026-01-01',
  'Acuerdo CSJ · vacancia judicial 2026'
)
on conflict do nothing;

-- Festivos nacionales 2026 CO
insert into legal_constants (key, value_jsonb, unit, jurisdiccion, effective_from, fuente)
values (
  'festivos_nacionales_2026',
  jsonb_build_array(
    '2026-01-01','2026-01-12','2026-03-23','2026-04-02','2026-04-03',
    '2026-05-01','2026-05-18','2026-06-08','2026-06-15','2026-07-06',
    '2026-07-20','2026-08-07','2026-08-17','2026-10-12','2026-11-02',
    '2026-11-16','2026-12-08','2026-12-25'
  ),
  'fechas',
  'CO',
  '2026-01-01',
  'Ley 51 de 1983 + Calendario CO'
)
on conflict do nothing;

commit;
