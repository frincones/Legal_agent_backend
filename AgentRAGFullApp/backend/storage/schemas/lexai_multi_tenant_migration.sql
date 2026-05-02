-- ============================================================
-- LexAI · Multi-tenant migration (additive over init.sql + legal_migration.sql)
--
-- Apply order: init.sql → legal_migration.sql → activity_migration.sql →
--              case_state_migration.sql → THIS FILE
--
-- Country-agnostic: works for Colombia (initial market) and México
-- (LATAM expansion). Corte filtering happens at query time via a
-- function parameter, not via hardcoded enum values.
-- ============================================================

create extension if not exists "pgcrypto";
create extension if not exists "vector";
create extension if not exists "pg_trgm";
create extension if not exists "unaccent";

-- ============================================================
-- ENUMS
-- ============================================================
do $$ begin
  if not exists (select 1 from pg_type where typname='firm_plan') then
    create type firm_plan as enum ('trial','despacho','estudio_pro','enterprise');
  end if;
  if not exists (select 1 from pg_type where typname='user_role') then
    create type user_role as enum ('admin','lawyer','paralegal','readonly');
  end if;
  if not exists (select 1 from pg_type where typname='matter_status') then
    create type matter_status as enum ('borrador','activo','en_espera','cerrado','archivado');
  end if;
  if not exists (select 1 from pg_type where typname='matter_priority') then
    create type matter_priority as enum ('alta','media','baja');
  end if;
  if not exists (select 1 from pg_type where typname='materia_legal') then
    -- Generic across CO + MX: laboral, civil, mercantil/comercial, penal,
    -- familiar, administrativo, constitucional (CO: tutelas / MX: amparos),
    -- fiscal, otro.
    create type materia_legal as enum (
      'laboral','civil','mercantil','comercial','penal','familiar',
      'administrativo','constitucional','fiscal','seguridad_social','otro'
    );
  end if;
  if not exists (select 1 from pg_type where typname='doc_kind') then
    create type doc_kind as enum (
      'demanda','contestacion','escrito','tutela','recurso','contrato',
      'sentencia','recibido','generado','otro'
    );
  end if;
  if not exists (select 1 from pg_type where typname='doc_status') then
    create type doc_status as enum ('pending','processing','completed','failed','superseded');
  end if;
  if not exists (select 1 from pg_type where typname='hitl_kind') then
    -- 'firma_digital' covers MX e.firma SAT and CO firma electrónica
    -- (Andes SCD, Certicámara, GSE).
    create type hitl_kind as enum (
      'email_externo','firma_digital','cita_jurisprudencia',
      'accion_financiera','sobrescribir','escrito_juzgado',
      'dato_sensible_habeas_data'
    );
  end if;
  if not exists (select 1 from pg_type where typname='hitl_decision') then
    create type hitl_decision as enum ('pending','approved','edited','rejected','timeout');
  end if;
  if not exists (select 1 from pg_type where typname='cita_estado') then
    create type cita_estado as enum ('verificada','no_encontrada','superada','sospechosa');
  end if;
  if not exists (select 1 from pg_type where typname='audit_action') then
    create type audit_action as enum (
      'auth.login','auth.logout','auth.mfa_enroll','auth.cedula_verify',
      'matter.create','matter.update','matter.archive',
      'document.upload','document.generate','document.export','document.summarize',
      'citation.insert','citation.verify','citation.reject',
      'liquidacion.compute',
      'voice.session_start','voice.session_end','voice.utterance',
      'agent.run','agent.tool_call','agent.error',
      'hitl.request','hitl.approve','hitl.edit','hitl.reject',
      'arco.request','arco.fulfill',
      'admin.role_change'
    );
  end if;
end $$;

-- ============================================================
-- FIRMS (root tenant)
-- ============================================================
create table if not exists firms (
  id            uuid primary key default gen_random_uuid(),
  razon_social  text not null,
  tax_id        text,                                -- NIT (CO) | RFC (MX) | RUT (CL)
  country       text not null default 'co',          -- ISO 3166-1 alpha-2 lower
  domain        text unique,
  plan          firm_plan not null default 'trial',
  trial_ends_at timestamptz,
  seats         int not null default 1,
  stripe_customer_id text,
  privacy_policy_version int default 1,
  dpa_signed_at timestamptz,
  zdr_enterprise boolean not null default false,
  region        text default 'us-east-1',
  metadata      jsonb default '{}'::jsonb,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);

-- ============================================================
-- USERS (Supabase auth.users → app profile)
-- ============================================================
create table if not exists users (
  id                  uuid primary key references auth.users(id) on delete cascade,
  firm_id             uuid not null references firms(id) on delete cascade,
  email               text not null,
  full_name           text not null,
  cedula_profesional  text,                          -- t.p. abogado (CO) / cédula SEP (MX)
  cedula_verified_at  timestamptz,
  cedula_payload      jsonb,
  role                user_role not null default 'lawyer',
  mfa_enrolled        boolean not null default false,
  avatar_url          text,
  preferences         jsonb default '{}'::jsonb,
  last_login_at       timestamptz,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
create index if not exists users_firm_idx on users (firm_id);
create unique index if not exists users_firm_email_uq on users (firm_id, email);

-- ============================================================
-- CLIENTS · neutral identity fields
-- ============================================================
create table if not exists clients (
  id            uuid primary key default gen_random_uuid(),
  firm_id       uuid not null references firms(id) on delete cascade,
  tipo          text not null check (tipo in ('persona_natural','persona_juridica')),
  nombre        text not null,
  tax_id        text,                                -- NIT/RFC/RUT
  personal_id   text,                                -- cédula ciudadanía / CURP / DNI
  email         text,
  telefono      text,
  domicilio     jsonb,
  vip           boolean default false,
  consent_at    timestamptz,                          -- habeas data CO / LFPDPPP MX
  consent_version int,
  consent_finalidades text[] default '{}',
  consent_voice_recording boolean default false,
  arco_requests jsonb default '[]'::jsonb,
  metadata      jsonb default '{}'::jsonb,
  created_by    uuid references users(id),
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);
create index if not exists clients_firm_name_idx on clients (firm_id, lower(nombre));
create index if not exists clients_name_trgm on clients using gin (nombre gin_trgm_ops);

-- ============================================================
-- MATTERS (casos)
-- ============================================================
create table if not exists matters (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  client_id       uuid not null references clients(id),
  display_id      text not null,
  titulo          text not null,
  materia         materia_legal not null,
  etapa_procesal  text,
  tribunal        text,
  juzgado         text,
  expediente      text,
  status          matter_status not null default 'activo',
  priority        matter_priority not null default 'media',
  owner_user_id   uuid references users(id),
  proxima_fecha   timestamptz,
  proxima_tipo    text,
  cuantia         numeric(14,2),                    -- moneda implícita por country
  cuantia_currency text default 'COP',              -- COP | MXN | USD
  pendientes      int not null default 0,
  is_demo         boolean not null default false,
  metadata        jsonb default '{}'::jsonb,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists matters_firm_status_idx on matters (firm_id, status, proxima_fecha);
create index if not exists matters_firm_materia_idx on matters (firm_id, materia);
create index if not exists matters_firm_owner_idx on matters (firm_id, owner_user_id);
create index if not exists matters_fts_idx on matters using gin (
  to_tsvector('spanish', coalesce(titulo,'') || ' ' || coalesce(expediente,''))
);

create table if not exists matter_parties (
  id          uuid primary key default gen_random_uuid(),
  firm_id     uuid not null references firms(id) on delete cascade,
  matter_id   uuid not null references matters(id) on delete cascade,
  rol         text not null,
  nombre      text not null,
  tax_id      text,
  client_id   uuid references clients(id),
  metadata    jsonb default '{}'::jsonb
);
create index if not exists matter_parties_matter_idx on matter_parties (matter_id);

create table if not exists matter_deadlines (
  id          uuid primary key default gen_random_uuid(),
  firm_id     uuid not null references firms(id) on delete cascade,
  matter_id   uuid not null references matters(id) on delete cascade,
  titulo      text not null,
  fecha       timestamptz not null,
  tipo        text,
  origen      text,
  completado  boolean default false,
  metadata    jsonb default '{}'::jsonb,
  created_at  timestamptz default now()
);
create index if not exists matter_deadlines_firm_fecha_idx on matter_deadlines (firm_id, fecha);
create index if not exists matter_deadlines_matter_idx on matter_deadlines (matter_id, fecha);

create table if not exists matter_timeline (
  id          uuid primary key default gen_random_uuid(),
  firm_id     uuid not null references firms(id) on delete cascade,
  matter_id   uuid not null references matters(id) on delete cascade,
  ts          timestamptz default now(),
  kind        text not null,
  actor_user_id uuid references users(id),
  agent_run_id uuid,
  payload     jsonb default '{}'::jsonb
);
create index if not exists matter_timeline_idx on matter_timeline (matter_id, ts desc);

create table if not exists matter_notes (
  id          uuid primary key default gen_random_uuid(),
  firm_id     uuid not null references firms(id) on delete cascade,
  matter_id   uuid not null references matters(id) on delete cascade,
  author_user_id uuid references users(id),
  body        text,
  voice_recording_url text,
  created_at  timestamptz default now()
);
create index if not exists matter_notes_idx on matter_notes (matter_id, created_at desc);

-- ============================================================
-- MATTER DOCUMENTS (per-case files; corpus jurídico vive en `documents`)
-- ============================================================
create table if not exists matter_documents (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  matter_id       uuid not null references matters(id) on delete cascade,
  kind            doc_kind not null,
  titulo          text not null,
  status          doc_status not null default 'pending',
  uploaded_by     uuid references users(id),
  storage_path    text,
  mime_type       text,
  byte_size       bigint,
  sha256          text,
  pages           int,
  ocr_done        boolean default false,
  resumen_ia      text,
  resumen_score   real,
  versioning      jsonb default '{}'::jsonb,
  ingest_doc_id   uuid,
  metadata        jsonb default '{}'::jsonb,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists matter_documents_idx on matter_documents (matter_id, created_at desc);
create index if not exists matter_documents_firm_kind_idx on matter_documents (firm_id, kind);

create table if not exists matter_document_versions (
  id                 uuid primary key default gen_random_uuid(),
  firm_id            uuid not null references firms(id) on delete cascade,
  matter_document_id uuid not null references matter_documents(id) on delete cascade,
  version            int not null,
  storage_path       text not null,
  sha256             text not null,
  generated_by       text,
  agent_run_id       uuid,
  diff_from_prev     jsonb,
  created_at         timestamptz default now()
);
-- Fill firm_id retroactively if the table was created without it
alter table matter_document_versions add column if not exists firm_id uuid references firms(id) on delete cascade;
create index if not exists matter_document_versions_firm_idx on matter_document_versions (firm_id);
create unique index if not exists matter_document_versions_uq on matter_document_versions (matter_document_id, version);

-- ============================================================
-- AGENT SESSIONS / RUNS / TOOL CALLS
-- ============================================================
create table if not exists agent_sessions (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid not null references users(id),
  matter_id       uuid references matters(id),
  channel         text not null check (channel in ('voice','text','cmdk')),
  status          text not null default 'active',
  cost_usd_total  numeric(10,4) default 0,
  tokens_in       bigint default 0,
  tokens_out      bigint default 0,
  cache_hit_rate  real,
  started_at      timestamptz default now(),
  ended_at        timestamptz,
  metadata        jsonb default '{}'::jsonb
);
create index if not exists agent_sessions_firm_idx on agent_sessions (firm_id, started_at desc);
create index if not exists agent_sessions_matter_idx on agent_sessions (matter_id);

create table if not exists agent_runs (
  id              uuid primary key default gen_random_uuid(),
  session_id      uuid not null references agent_sessions(id) on delete cascade,
  firm_id         uuid not null references firms(id) on delete cascade,
  matter_id       uuid references matters(id),
  intent          text,
  user_input      text,
  user_input_audio_url text,
  model_router    text,
  langgraph_thread_id text,
  ttft_ms         int,
  e2e_ms          int,
  voice_e2e_ms    int,
  faithfulness    real,
  citations_count int default 0,
  citations_verified_count int default 0,
  hallucinated_blocked int default 0,
  finish_reason   text,
  error           text,
  metadata        jsonb default '{}'::jsonb,
  started_at      timestamptz default now(),
  ended_at        timestamptz
);
create index if not exists agent_runs_session_idx on agent_runs (session_id, started_at desc);
create index if not exists agent_runs_firm_intent_idx on agent_runs (firm_id, intent);

create table if not exists agent_tool_calls (
  id              uuid primary key default gen_random_uuid(),
  agent_run_id    uuid not null references agent_runs(id) on delete cascade,
  firm_id         uuid not null references firms(id) on delete cascade,
  tool_name       text not null,
  status          text not null default 'queued',
  input           jsonb,
  output          jsonb,
  duration_ms     int,
  parallel_group  int,
  started_at      timestamptz default now(),
  ended_at        timestamptz
);
create index if not exists agent_tool_calls_run_idx on agent_tool_calls (agent_run_id, started_at);

-- ============================================================
-- HITL INTERRUPTS (F5)
-- ============================================================
create table if not exists hitl_interrupts (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  agent_run_id    uuid references agent_runs(id) on delete cascade,
  user_id         uuid not null references users(id),
  matter_id       uuid references matters(id),
  kind            hitl_kind not null,
  payload         jsonb not null,
  decision        hitl_decision not null default 'pending',
  decision_user_id uuid references users(id),
  decision_payload jsonb,
  expires_at      timestamptz,
  created_at      timestamptz default now(),
  decided_at      timestamptz
);
create index if not exists hitl_firm_decision_idx on hitl_interrupts (firm_id, decision, created_at desc);
create index if not exists hitl_matter_idx on hitl_interrupts (matter_id);

-- ============================================================
-- DOCUMENT CITATIONS (auditable jurisprudencia inserted)
-- ============================================================
create table if not exists document_citations (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  matter_document_id uuid references matter_documents(id) on delete cascade,
  agent_run_id    uuid references agent_runs(id),
  juris_id        uuid,                                -- → public.jurisprudencia.id
  citation_ref    text,                                -- e.g. "T-388/2019" o "2a./J. 14/2024"
  rubro_inserted  text,
  estado          cita_estado not null,
  match_score     real,
  inserted_at     timestamptz default now(),
  verified_at     timestamptz,
  approved_by     uuid references users(id)
);
create index if not exists doc_citations_firm_idx on document_citations (firm_id);
create index if not exists doc_citations_juris_idx on document_citations (juris_id);
create index if not exists doc_citations_doc_idx on document_citations (matter_document_id);

-- ============================================================
-- LIQUIDACIÓN (F7) · neutral; CO uses CST formulas, MX uses LFT
-- ============================================================
create table if not exists liquidacion_calculations (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  matter_id       uuid references matters(id),
  user_id         uuid not null references users(id),
  trabajador_nombre text,
  fecha_ingreso   date not null,
  fecha_terminacion date not null,
  salario_diario  numeric(12,2) not null,
  salario_integrado numeric(12,2),
  causa           text not null,
  variables       jsonb not null,
  resultado       jsonb not null,
  total_amount    numeric(14,2) not null,
  total_currency  text not null default 'COP',
  formulas_version text not null,                      -- 'cst-co-2024-q4' | 'lft-mx-2024-q4'
  computed_at     timestamptz default now()
);
create index if not exists liquidacion_firm_idx on liquidacion_calculations (firm_id, computed_at desc);

-- ============================================================
-- VOICE TELEMETRY
-- ============================================================
create table if not exists voice_sessions (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid not null references users(id),
  agent_session_id uuid references agent_sessions(id),
  wake_word_used  boolean,
  ptt_used        boolean,
  duration_ms     int,
  utterances      int default 0,
  bargeins        int default 0,
  ttfa_p50_ms     int,
  voice_e2e_p50_ms int,
  network_quality text,
  metadata        jsonb default '{}'::jsonb,
  started_at      timestamptz default now(),
  ended_at        timestamptz
);
create index if not exists voice_sessions_firm_idx on voice_sessions (firm_id, started_at desc);

-- ============================================================
-- AUDIT LOG (F15)
-- ============================================================
create table if not exists audit_log (
  id          bigserial primary key,
  ts          timestamptz default now(),
  firm_id     uuid,
  user_id     uuid,
  action      audit_action not null,
  target_type text,
  target_id   uuid,
  payload     jsonb default '{}'::jsonb,
  ip_inet     inet,
  user_agent  text,
  sha256      text not null,
  prev_sha256 text
);
create index if not exists audit_firm_ts_idx on audit_log (firm_id, ts desc);
create index if not exists audit_action_ts_idx on audit_log (action, ts desc);

create or replace function audit_immutable() returns trigger language plpgsql as $$
begin
  raise exception 'audit_log is append-only (UPDATE/DELETE prohibido)';
end;
$$;

drop trigger if exists audit_log_no_update on audit_log;
create trigger audit_log_no_update before update on audit_log
  for each row execute function audit_immutable();

drop trigger if exists audit_log_no_delete on audit_log;
create trigger audit_log_no_delete before delete on audit_log
  for each row execute function audit_immutable();

-- ============================================================
-- BILLING / NSM
-- ============================================================
create table if not exists billing_subscriptions (
  firm_id           uuid primary key references firms(id) on delete cascade,
  stripe_subscription_id text unique,
  status            text,
  plan              firm_plan,
  seats             int,
  current_period_start timestamptz,
  current_period_end timestamptz,
  cancel_at         timestamptz,
  metadata          jsonb default '{}'::jsonb
);

create table if not exists nsm_daily (
  firm_id     uuid not null references firms(id) on delete cascade,
  day         date not null,
  documentos_verificados int default 0,
  voice_commands int default 0,
  horas_ahorradas numeric(6,2),
  cost_usd    numeric(10,4),
  primary key (firm_id, day)
);

-- ============================================================
-- EXTEND EXISTING TABLES with firm_id (multi-tenant)
-- ============================================================
alter table documents          add column if not exists firm_id uuid references firms(id);
alter table chunks             add column if not exists firm_id uuid references firms(id);
alter table conversations      add column if not exists firm_id uuid references firms(id);
alter table conversation_chunks add column if not exists firm_id uuid references firms(id);

create index if not exists documents_firm_idx on documents (firm_id);
create index if not exists chunks_firm_idx on chunks (firm_id);
create index if not exists conversations_firm_idx on conversations (firm_id);

-- normas: corpus público (firm_id nullable) + extension fields
alter table normas add column if not exists firm_id uuid;
create index if not exists normas_firm_idx on normas (firm_id);

-- jurisprudencia: extend with vigencia / superada_por for citation registry
alter table jurisprudencia add column if not exists firm_id uuid;
alter table jurisprudencia add column if not exists rubro text;
alter table jurisprudencia add column if not exists vigencia text default 'vigente';
alter table jurisprudencia add column if not exists superada_por uuid references jurisprudencia(id);
alter table jurisprudencia add column if not exists sha256 text;

create index if not exists juris_firm_idx on jurisprudencia (firm_id);
create index if not exists juris_vigencia_idx on jurisprudencia (vigencia);
create index if not exists juris_rubro_trgm on jurisprudencia using gin (rubro gin_trgm_ops);

-- ============================================================
-- FUNCTION: match_juris (hybrid search over `jurisprudencia`)
-- Country-agnostic; pass corte filter from the application layer.
-- For Colombia: filter_corte ∈ ('CORTE_CONSTITUCIONAL','CORTE_SUPREMA','CONSEJO_ESTADO').
-- For México   : filter_corte = 'SCJN_MX'.
-- Pass NULL to search all courts (cross-border firms).
-- ============================================================
create or replace function match_juris(
  query_embedding vector(1536),
  query_text text default '',
  filter_corte text default null,
  filter_tipo text default null,
  match_count int default 8,
  text_weight float default 0.45,
  similarity_threshold float default 0.10,
  only_vigente boolean default true
) returns table (
  juris_id uuid,
  corte text,
  citation_ref text,                   -- numero o serie
  rubro text,
  vigencia text,
  url_oficial text,
  ratio_decidendi text,
  combined_score float,
  vector_similarity float,
  text_similarity float
) language sql stable as $$
  with v as (
    select
      j.id as juris_id,
      j.corte,
      j.numero as citation_ref,
      coalesce(j.rubro, j.decision) as rubro,
      coalesce(j.vigencia, 'vigente') as vigencia,
      j.fuente_url as url_oficial,
      j.ratio_decidendi,
      1 - (j.embedding <=> query_embedding) as vs
    from jurisprudencia j
    where j.embedding is not null
      and (filter_corte is null or j.corte = filter_corte)
      and (filter_tipo is null or j.tipo_sentencia = filter_tipo)
      and (not only_vigente or coalesce(j.vigencia,'vigente') = 'vigente')
      and 1 - (j.embedding <=> query_embedding) > similarity_threshold
  ), t as (
    select v.*, ts_rank_cd(
      to_tsvector('spanish', coalesce(v.rubro,'') || ' ' || coalesce(v.ratio_decidendi,'')),
      plainto_tsquery('spanish', query_text)
    ) as ts
    from v
  )
  select
    t.juris_id, t.corte, t.citation_ref, t.rubro, t.vigencia, t.url_oficial,
    t.ratio_decidendi,
    ((1.0 - text_weight) * t.vs + text_weight * coalesce(t.ts, 0)) as combined_score,
    t.vs, coalesce(t.ts, 0)
  from t
  order by combined_score desc
  limit match_count;
$$;

-- ============================================================
-- ROW LEVEL SECURITY (F14 — 0 cross-tenant leaks)
-- ============================================================
alter table firms                    enable row level security;
alter table users                    enable row level security;
alter table clients                  enable row level security;
alter table matters                  enable row level security;
alter table matter_parties           enable row level security;
alter table matter_deadlines         enable row level security;
alter table matter_timeline          enable row level security;
alter table matter_notes             enable row level security;
alter table matter_documents         enable row level security;
alter table matter_document_versions enable row level security;
alter table agent_sessions           enable row level security;
alter table agent_runs               enable row level security;
alter table agent_tool_calls         enable row level security;
alter table hitl_interrupts          enable row level security;
alter table document_citations       enable row level security;
alter table liquidacion_calculations enable row level security;
alter table voice_sessions           enable row level security;
alter table audit_log                enable row level security;
alter table billing_subscriptions    enable row level security;
alter table nsm_daily                enable row level security;

-- Helper: extract firm_id from custom JWT claim
create or replace function auth_firm_id() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::json ->> 'firm_id','')::uuid;
$$;

-- Apply firm_id RLS to every tenant table
do $$
declare t text;
declare tables text[] := array[
  'users','clients','matters','matter_parties','matter_deadlines',
  'matter_timeline','matter_notes','matter_documents','matter_document_versions',
  'agent_sessions','agent_runs','agent_tool_calls','hitl_interrupts',
  'document_citations','liquidacion_calculations','voice_sessions',
  'audit_log','billing_subscriptions','nsm_daily'
];
begin
  foreach t in array tables loop
    execute format('drop policy if exists %1$I_select on %1$I', t);
    execute format('drop policy if exists %1$I_modify on %1$I', t);
    execute format($f$
      create policy %1$I_select on %1$I for select
      using (firm_id = auth_firm_id() or auth.role() = 'service_role')
    $f$, t);
    execute format($f$
      create policy %1$I_modify on %1$I for all
      using (firm_id = auth_firm_id() or auth.role() = 'service_role')
      with check (firm_id = auth_firm_id() or auth.role() = 'service_role')
    $f$, t);
  end loop;
end $$;

drop policy if exists firms_select on firms;
create policy firms_select on firms for select
using (id = auth_firm_id() or auth.role() = 'service_role');

revoke insert on audit_log from authenticated, anon;

-- ============================================================
-- TRIGGERS · firm_id auto-fill from JWT
-- ============================================================
create or replace function set_firm_id_from_jwt() returns trigger language plpgsql as $$
begin
  if new.firm_id is null then
    new.firm_id := auth_firm_id();
  end if;
  return new;
end;
$$;

do $$
declare t text;
declare tables text[] := array[
  'matters','clients','matter_parties','matter_deadlines','matter_timeline',
  'matter_notes','matter_documents','agent_sessions','agent_runs',
  'agent_tool_calls','hitl_interrupts','document_citations',
  'liquidacion_calculations','voice_sessions'
];
begin
  foreach t in array tables loop
    execute format('drop trigger if exists trg_%1$s_firm_id on %1$s', t);
    execute format('create trigger trg_%1$s_firm_id before insert on %1$s for each row execute function set_firm_id_from_jwt()', t);
  end loop;
end $$;

-- ============================================================
-- updated_at trigger helper
-- ============================================================
create or replace function tg_set_updated_at() returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

do $$
declare t text;
declare tables text[] := array['firms','users','clients','matters','matter_documents'];
begin
  foreach t in array tables loop
    execute format('drop trigger if exists trg_%1$s_updated_at on %1$s', t);
    execute format('create trigger trg_%1$s_updated_at before update on %1$s for each row execute function tg_set_updated_at()', t);
  end loop;
end $$;

-- ============================================================
-- DONE. Post-apply checklist:
-- 1. Configure Supabase Auth Hook (custom_access_token_hook) injecting
--    firm_id, role, cedula_profesional into JWT claims.
-- 2. Create Storage buckets: matters/, voice/, exports/ (all private RLS).
-- 3. RLS smoke test: 2 firms × 2 users → cross-read returns 0 rows.
-- ============================================================
