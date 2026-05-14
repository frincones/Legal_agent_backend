-- ============================================================
-- LexAI · Sprint 21 · M29 Evidence Authenticity Checker
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- 3 tablas firm-scoped:
--   evidence_validations    · cross-check identidad vs Registro Civil/RUE/RUT
--   evidence_inconsistencies · análisis LLM de inconsistencias en un doc
--   evidence_scores         · score probatorio 0-100 + factores
-- ============================================================

-- ------------------------------------------------------------
-- 1. evidence_validations
-- ------------------------------------------------------------
create table if not exists evidence_validations (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete cascade,
  matter_document_id uuid references matter_documents(id) on delete set null,
  -- Sujeto
  subject_kind      text not null
    check (subject_kind in ('persona','empresa')),
  subject_id_kind   text not null
    check (subject_id_kind in ('cedula','nit','pasaporte','rut','otro')),
  subject_id_value  text not null,
  subject_name      text,
  -- Providers consultados
  providers_used    text[] default array[]::text[],
  -- Resultado
  status            text not null default 'pending'
    check (status in ('pending','matched','mismatch','partial','not_found','error')),
  results           jsonb default '{}'::jsonb,                 -- {provider: {ok, payload, source, ts}}
  mismatches        jsonb default '[]'::jsonb,                 -- [{field, expected, found, severity}]
  -- Audit
  validated_by      uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists ev_val_firm_matter_idx
  on evidence_validations (firm_id, matter_id, created_at desc);
create index if not exists ev_val_subject_idx
  on evidence_validations (firm_id, subject_id_value);
create index if not exists ev_val_doc_idx
  on evidence_validations (matter_document_id, created_at desc) where matter_document_id is not null;

-- ------------------------------------------------------------
-- 2. evidence_inconsistencies
-- ------------------------------------------------------------
create table if not exists evidence_inconsistencies (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete cascade,
  matter_document_id uuid not null references matter_documents(id) on delete cascade,
  -- Input cache
  document_hash     text,                                       -- evita re-analizar lo mismo
  -- Output del LLM
  inconsistencies   jsonb default '[]'::jsonb,                  -- [{type, severity, location, description, suggestion}]
  total_count       int not null default 0,
  high_severity_count int not null default 0,
  summary           text,
  -- Audit
  model_used        text,
  analyzed_by       uuid references users(id) on delete set null,
  analyzed_at       timestamptz default now()
);
create index if not exists ev_inc_firm_doc_idx
  on evidence_inconsistencies (firm_id, matter_document_id, analyzed_at desc);

-- ------------------------------------------------------------
-- 3. evidence_scores · score probatorio
-- ------------------------------------------------------------
create table if not exists evidence_scores (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete cascade,
  matter_document_id uuid not null references matter_documents(id) on delete cascade,
  -- Score
  probative_score   int not null default 0
    check (probative_score between 0 and 100),
  level             text not null
    check (level in ('fuerte','medio','debil','cuestionable')),
  summary           text,
  -- Breakdown
  positive_factors  jsonb default '[]'::jsonb,                  -- [{factor, weight, note}]
  negative_factors  jsonb default '[]'::jsonb,
  recommendations   jsonb default '[]'::jsonb,
  -- Sources
  validation_id     uuid references evidence_validations(id) on delete set null,
  inconsistency_id  uuid references evidence_inconsistencies(id) on delete set null,
  -- Audit
  computed_by       uuid references users(id) on delete set null,
  computed_at       timestamptz default now(),
  reviewed_by       uuid references users(id) on delete set null,
  reviewed_at       timestamptz
);
create index if not exists ev_score_firm_doc_idx
  on evidence_scores (firm_id, matter_document_id, computed_at desc);
create index if not exists ev_score_matter_idx
  on evidence_scores (matter_id, computed_at desc) where matter_id is not null;

-- ============================================================
-- RLS firm-scoped
-- ============================================================
alter table evidence_validations    enable row level security;
alter table evidence_inconsistencies enable row level security;
alter table evidence_scores         enable row level security;

drop policy if exists ev_val_select on evidence_validations;
drop policy if exists ev_val_modify on evidence_validations;
create policy ev_val_select on evidence_validations for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ev_val_modify on evidence_validations for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ev_inc_select on evidence_inconsistencies;
drop policy if exists ev_inc_modify on evidence_inconsistencies;
create policy ev_inc_select on evidence_inconsistencies for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ev_inc_modify on evidence_inconsistencies for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ev_score_select on evidence_scores;
drop policy if exists ev_score_modify on evidence_scores;
create policy ev_score_select on evidence_scores for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ev_score_modify on evidence_scores for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_ev_val_firm_id on evidence_validations;
create trigger trg_ev_val_firm_id before insert on evidence_validations
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_ev_val_updated_at on evidence_validations;
create trigger trg_ev_val_updated_at before update on evidence_validations
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_ev_inc_firm_id on evidence_inconsistencies;
create trigger trg_ev_inc_firm_id before insert on evidence_inconsistencies
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ev_score_firm_id on evidence_scores;
create trigger trg_ev_score_firm_id before insert on evidence_scores
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPCs
-- ============================================================

-- Última validación de identidad para un documento o sujeto
create or replace function lexai_latest_identity_validation(
  p_firm_id            uuid,
  p_matter_document_id uuid default null,
  p_subject_id_value   text default null
) returns table (
  id uuid,
  subject_kind text,
  subject_id_kind text,
  subject_id_value text,
  subject_name text,
  status text,
  providers_used text[],
  results jsonb,
  mismatches jsonb,
  created_at timestamptz
)
language sql stable as $$
  select id, subject_kind, subject_id_kind, subject_id_value, subject_name,
         status, providers_used, results, mismatches, created_at
    from evidence_validations
   where firm_id = p_firm_id
     and (p_matter_document_id is null or matter_document_id = p_matter_document_id)
     and (p_subject_id_value is null or subject_id_value = p_subject_id_value)
   order by created_at desc
   limit 1;
$$;

-- Última detección de inconsistencias para un documento
create or replace function lexai_latest_inconsistencies(
  p_firm_id            uuid,
  p_matter_document_id uuid
) returns table (
  id uuid,
  inconsistencies jsonb,
  total_count int,
  high_severity_count int,
  summary text,
  analyzed_at timestamptz
)
language sql stable as $$
  select id, inconsistencies, total_count, high_severity_count, summary, analyzed_at
    from evidence_inconsistencies
   where firm_id = p_firm_id and matter_document_id = p_matter_document_id
   order by analyzed_at desc
   limit 1;
$$;

-- Último score probatorio
create or replace function lexai_latest_probative_score(
  p_firm_id            uuid,
  p_matter_document_id uuid
) returns table (
  id uuid,
  probative_score int,
  level text,
  summary text,
  positive_factors jsonb,
  negative_factors jsonb,
  recommendations jsonb,
  validation_id uuid,
  inconsistency_id uuid,
  computed_at timestamptz,
  reviewed_at timestamptz
)
language sql stable as $$
  select id, probative_score, level, summary, positive_factors, negative_factors,
         recommendations, validation_id, inconsistency_id, computed_at, reviewed_at
    from evidence_scores
   where firm_id = p_firm_id and matter_document_id = p_matter_document_id
   order by computed_at desc
   limit 1;
$$;

-- Stats de evidencia por matter
create or replace function lexai_evidence_stats(
  p_firm_id   uuid,
  p_matter_id uuid
) returns jsonb language sql stable as $$
  select jsonb_build_object(
    'validations_total', (
      select count(*) from evidence_validations
       where firm_id = p_firm_id and matter_id = p_matter_id
    ),
    'validations_mismatched', (
      select count(*) from evidence_validations
       where firm_id = p_firm_id and matter_id = p_matter_id
         and status in ('mismatch','partial')
    ),
    'inconsistencies_high_count', coalesce((
      select sum(high_severity_count)::int from evidence_inconsistencies
       where firm_id = p_firm_id and matter_id = p_matter_id
    ), 0),
    'avg_probative_score', coalesce((
      select round(avg(probative_score), 1) from evidence_scores
       where firm_id = p_firm_id and matter_id = p_matter_id
    ), 0),
    'docs_scored', (
      select count(distinct matter_document_id) from evidence_scores
       where firm_id = p_firm_id and matter_id = p_matter_id
    )
  );
$$;
