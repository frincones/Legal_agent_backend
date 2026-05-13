-- ============================================================
-- LexAI · Sprint 11 · Document AI v2 · Contract Analyzer + Doc Q&A + Compare
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. contract_analyses · resultado del Contract Analyzer por documento
-- ------------------------------------------------------------
create table if not exists contract_analyses (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_document_id uuid not null references matter_documents(id) on delete cascade,
  matter_id         uuid references matters(id) on delete set null,
  contract_type     text,                                    -- 'arrendamiento','prestacion_servicios','laboral','compraventa','mandato','otro'
  parties           jsonb default '[]'::jsonb,               -- [{rol,nombre,tax_id,personal_id}]
  resumen_ejecutivo text,
  fecha_inicio      date,
  fecha_fin         date,
  monto_total_cop   numeric(14,2),
  moneda            text default 'COP',
  jurisdiccion      text,                                    -- 'colombia','mexico','arbitral'
  ley_aplicable     text,
  risk_score        int default 0 check (risk_score between 0 and 100),
  status            text not null default 'completed'
    check (status in ('pending','analyzing','completed','failed')),
  llm_model         text default 'gpt-4o',
  prompt_tokens     int default 0,
  completion_tokens int default 0,
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists contract_analyses_doc_idx
  on contract_analyses (matter_document_id, created_at desc);
create index if not exists contract_analyses_firm_idx
  on contract_analyses (firm_id, created_at desc);

-- ------------------------------------------------------------
-- 2. contract_clauses · cláusulas clasificadas del contrato
-- ------------------------------------------------------------
create table if not exists contract_clauses (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  analysis_id       uuid not null references contract_analyses(id) on delete cascade,
  category          text not null,                          -- 'objeto','plazo','precio','penalidades','indemnidad',
                                                            -- 'terminacion','jurisdiccion','confidencialidad',
                                                            -- 'no_competencia','fuerza_mayor','garantias','otro'
  numero            text,                                    -- "Cláusula Quinta", "Art. 3"
  titulo            text,
  texto             text not null,
  page_number       int,
  importance        text default 'normal' check (importance in ('critica','alta','normal','baja')),
  position          int not null default 0,
  created_at        timestamptz default now()
);
create index if not exists contract_clauses_analysis_idx
  on contract_clauses (analysis_id, position);
create index if not exists contract_clauses_category_idx
  on contract_clauses (analysis_id, category);

-- ------------------------------------------------------------
-- 3. contract_risks · riesgos detectados con redlining
-- ------------------------------------------------------------
create table if not exists contract_risks (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  analysis_id       uuid not null references contract_analyses(id) on delete cascade,
  clause_id         uuid references contract_clauses(id) on delete set null,
  kind              text not null,                          -- 'clausula_abusiva','ambiguedad','faltante',
                                                            -- 'desbalance','penalidad_excesiva','jurisdiccion_remota',
                                                            -- 'indemnidad_unilateral','otro'
  severity          text not null default 'medio'
    check (severity in ('bajo','medio','alto','critico')),
  title             text not null,
  description       text not null,                          -- por qué es riesgo
  suggested_action  text,                                   -- qué hacer
  suggested_text    text,                                   -- texto sugerido (redlining)
  citations         jsonb default '[]'::jsonb,              -- refs normativas (CST, CGP, Código Comercio)
  status            text not null default 'open'
    check (status in ('open','accepted','dismissed','negotiating')),
  resolved_at       timestamptz,
  resolved_by       uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists contract_risks_analysis_idx
  on contract_risks (analysis_id, severity, created_at desc);
create index if not exists contract_risks_firm_severity_idx
  on contract_risks (firm_id, severity, status) where status = 'open';

-- ------------------------------------------------------------
-- 4. doc_qa_sessions · chats sobre uno o varios documentos
-- ------------------------------------------------------------
create table if not exists doc_qa_sessions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  matter_id         uuid references matters(id) on delete set null,
  scope_kind        text not null default 'document'
    check (scope_kind in ('document','matter','custom')),
  scope_document_ids uuid[] default '{}',                   -- matter_documents.id[]
  title             text not null default 'Consulta',
  message_count     int not null default 0,
  llm_model         text default 'gpt-4o',
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists doc_qa_sessions_firm_idx
  on doc_qa_sessions (firm_id, updated_at desc);
create index if not exists doc_qa_sessions_matter_idx
  on doc_qa_sessions (matter_id, updated_at desc) where matter_id is not null;

-- ------------------------------------------------------------
-- 5. doc_qa_messages · turnos del chat con citas
-- ------------------------------------------------------------
create table if not exists doc_qa_messages (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  session_id        uuid not null references doc_qa_sessions(id) on delete cascade,
  role              text not null check (role in ('user','assistant','system')),
  content           text not null,
  citations         jsonb default '[]'::jsonb,              -- [{document_id, page, snippet}]
  prompt_tokens     int default 0,
  completion_tokens int default 0,
  created_at        timestamptz default now()
);
create index if not exists doc_qa_messages_session_idx
  on doc_qa_messages (session_id, created_at);

-- ------------------------------------------------------------
-- 6. doc_comparisons · diff almacenado entre 2 documentos
-- ------------------------------------------------------------
create table if not exists doc_comparisons (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  document_a_id     uuid not null references matter_documents(id) on delete cascade,
  document_b_id     uuid not null references matter_documents(id) on delete cascade,
  summary           text,
  diff_html         text,                                    -- HTML pre-renderizado
  diff_json         jsonb default '{}'::jsonb,               -- estructurado por bloques
  added_blocks      int default 0,
  removed_blocks    int default 0,
  changed_blocks    int default 0,
  semantic_summary  text,                                    -- LLM-generated narrative
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists doc_comparisons_firm_idx
  on doc_comparisons (firm_id, created_at desc);
create index if not exists doc_comparisons_pair_idx
  on doc_comparisons (document_a_id, document_b_id);

-- ============================================================
-- RLS
-- ============================================================
alter table contract_analyses enable row level security;
alter table contract_clauses  enable row level security;
alter table contract_risks    enable row level security;
alter table doc_qa_sessions   enable row level security;
alter table doc_qa_messages   enable row level security;
alter table doc_comparisons   enable row level security;

drop policy if exists ca_select on contract_analyses;
drop policy if exists ca_modify on contract_analyses;
create policy ca_select on contract_analyses for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ca_modify on contract_analyses for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists cc_select on contract_clauses;
drop policy if exists cc_modify on contract_clauses;
create policy cc_select on contract_clauses for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cc_modify on contract_clauses for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists cr_select on contract_risks;
drop policy if exists cr_modify on contract_risks;
create policy cr_select on contract_risks for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cr_modify on contract_risks for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists qa_sessions_select on doc_qa_sessions;
drop policy if exists qa_sessions_modify on doc_qa_sessions;
create policy qa_sessions_select on doc_qa_sessions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy qa_sessions_modify on doc_qa_sessions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists qa_messages_select on doc_qa_messages;
drop policy if exists qa_messages_modify on doc_qa_messages;
create policy qa_messages_select on doc_qa_messages for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy qa_messages_modify on doc_qa_messages for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists dc_select on doc_comparisons;
drop policy if exists dc_modify on doc_comparisons;
create policy dc_select on doc_comparisons for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy dc_modify on doc_comparisons for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_ca_firm_id on contract_analyses;
create trigger trg_ca_firm_id before insert on contract_analyses
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_ca_updated_at on contract_analyses;
create trigger trg_ca_updated_at before update on contract_analyses
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_cc_firm_id on contract_clauses;
create trigger trg_cc_firm_id before insert on contract_clauses
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_cr_firm_id on contract_risks;
create trigger trg_cr_firm_id before insert on contract_risks
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_qa_sessions_firm_id on doc_qa_sessions;
create trigger trg_qa_sessions_firm_id before insert on doc_qa_sessions
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_qa_sessions_updated_at on doc_qa_sessions;
create trigger trg_qa_sessions_updated_at before update on doc_qa_sessions
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_qa_messages_firm_id on doc_qa_messages;
create trigger trg_qa_messages_firm_id before insert on doc_qa_messages
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_dc_firm_id on doc_comparisons;
create trigger trg_dc_firm_id before insert on doc_comparisons
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPC · stats firm-level
-- ============================================================
create or replace function lexai_contract_stats(p_firm_id uuid default null)
returns jsonb language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id)
  select jsonb_build_object(
    'analyses_total', (select count(*) from contract_analyses where firm_id = (select id from f)),
    'risks_open',     (select count(*) from contract_risks where firm_id = (select id from f) and status = 'open'),
    'risks_critical', (select count(*) from contract_risks
                         where firm_id = (select id from f) and severity = 'critico' and status = 'open'),
    'qa_sessions',    (select count(*) from doc_qa_sessions where firm_id = (select id from f)),
    'qa_messages',    (select count(*) from doc_qa_messages where firm_id = (select id from f)),
    'avg_risk_score', coalesce((select round(avg(risk_score), 1) from contract_analyses
                                  where firm_id = (select id from f)), 0)
  );
$$;
