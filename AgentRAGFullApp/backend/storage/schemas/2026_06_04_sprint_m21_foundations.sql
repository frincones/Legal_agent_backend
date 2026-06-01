-- ============================================================================
-- Sprint M21 · Fundaciones arquitecturales (Claude-for-Legal-parity)
-- ============================================================================
-- 7 tablas nuevas que el patron Anthropic Claude for Legal asume como standard
-- y que LexAI necesita para alcanzar paridad:
--
--   1. firms_profile               (company profile compartido)
--   2. practice_profile_sections   (practice profile multi-seccion estructurado)
--   3. matters_workspace           (workspaces por caso)
--   4. matter_history              (audit append-only por matter)
--   5. firm_seed_documents         (docs ejemplares subidos en cold-start)
--   6. firm_guardrails             (config G1-G5 por firm)
--   7. cold_start_sessions         (state machine onboarding)
--
-- TODAS idempotentes (CREATE IF NOT EXISTS + ALTER IF NOT EXISTS).
-- TODAS con RLS por firm_id (multi-tenant isolation).
-- TODAS con indices comunes para performance.
-- NO modifica tablas existentes para evitar regresiones.
-- NO afecta firm_skills / firm_playbook / matters / tool_call_audit existentes.
-- ============================================================================

begin;

-- ============================================================
-- TABLA 1: firms_profile (company-level shared profile)
-- ============================================================
-- Datos basicos de la firma que TODOS los plugins/areas leen (industria,
-- tamano, ubicacion). Evita duplicar info en cada practice_profile.

create table if not exists firms_profile (
    firm_id            uuid primary key references firms(id) on delete cascade,
    company_name       text not null,
    legal_name         text,                        -- razon social completa
    nit                text,                        -- NIT con DV
    industry           text,                        -- 'legal_services', 'finance', 'real_estate', etc.
    practice_setting   text default 'firm'          -- 'solo' | 'firm' | 'in_house' | 'government' | 'clinic'
                       check (practice_setting in ('solo', 'firm', 'in_house', 'government', 'clinic')),
    jurisdiction       text default 'CO',           -- 'CO', 'MX', 'CO,MX', etc.
    size_employees     int,
    pain_points_md     text,                        -- "lo que mas duele" en palabras del lawyer
    metadata           jsonb default '{}'::jsonb,
    created_at         timestamptz default now(),
    updated_at         timestamptz default now()
);

comment on table firms_profile is 'M21.S1: Company profile compartido entre todas las areas de practica del firm.';

create index if not exists idx_firms_profile_jurisdiction on firms_profile (jurisdiction);

-- ============================================================
-- TABLA 2: practice_profile_sections (multi-seccion estructurado)
-- ============================================================
-- Reemplaza firm_playbook.raw_md (que es texto libre sin estructura).
-- Cada area de practica (notarial, judicial, corporate...) tiene su propio
-- conjunto de secciones (who_we_are, playbook, escalation, house_style, etc).
-- Los SKILLs pueden leer secciones especificas en vez de todo el playbook.
--
-- IMPORTANTE: firm_playbook se mantiene en BD (NO se borra) para backward
-- compatibility. Migracion data sera en Sprint 2 cuando se haga el cold-start.

create table if not exists practice_profile_sections (
    id                 uuid primary key default gen_random_uuid(),
    firm_id            uuid not null references firms(id) on delete cascade,
    area               text not null,               -- 'notarial', 'judicial_civil', 'judicial_laboral', etc.
    section_key        text not null,               -- 'who_we_are', 'playbook', 'escalation', 'house_style', 'integrations', 'seed_docs_summary'
    section_subkey     text,                        -- opcional: 'playbook.sales_side.nda_positions'
    content_md         text not null,               -- markdown content de la seccion
    source             text default 'cold_start',   -- 'cold_start' | 'manual_edit' | 'auto_learned'
    version            int default 1,
    created_at         timestamptz default now(),
    updated_at         timestamptz default now(),
    constraint uq_practice_profile_section unique (firm_id, area, section_key, section_subkey)
);

comment on table practice_profile_sections is 'M21.S1: Practice profile por firm y area, particionado en secciones para que SKILLs lean lo que necesitan sin cargar todo el playbook.';

create index if not exists idx_practice_profile_firm_area on practice_profile_sections (firm_id, area);
create index if not exists idx_practice_profile_section on practice_profile_sections (firm_id, area, section_key);

-- ============================================================
-- TABLA 3: matters_workspace (workspaces por caso)
-- ============================================================
-- Cada caso (matter) tiene un workspace con su contexto, side, jurisdiccion,
-- phase, theory. Los SKILLs leen el matter activo y appendean a history.
--
-- NOTA: tabla `matters` LEGACY existe (campo matter_id en generations).
-- Esta es la NUEVA workspace structured. No reemplaza la legacy todavia,
-- coexisten hasta que Sprint 2 migre los queries.

create table if not exists matters_workspace (
    matter_id          uuid primary key default gen_random_uuid(),
    firm_id            uuid not null references firms(id) on delete cascade,
    slug               text not null,               -- 'bigcorp-msa', 'transportes-sas-2024'
    title              text not null,
    area               text not null,               -- 'notarial', 'judicial_laboral', etc.
    side               text,                        -- 'demandante', 'demandado', 'comprador', 'vendedor', 'neutral'
    jurisdiction       text default 'CO',
    phase              text,                        -- 'pre_litigation', 'discovery', 'trial', 'appeal', 'closed'
    theory_md          text,                        -- teoria del caso, free-form markdown
    opposing_party     text,                        -- contraparte
    client_name        text,
    matter_metadata    jsonb default '{}'::jsonb,
    active             boolean default true,        -- soft-close
    created_by_user_id uuid references users(id),
    created_at         timestamptz default now(),
    updated_at         timestamptz default now(),
    closed_at          timestamptz,
    constraint uq_matter_firm_slug unique (firm_id, slug)
);

comment on table matters_workspace is 'M21.S1: Workspace por caso. Cada matter tiene contexto + side + phase + history asociado.';

create index if not exists idx_matter_workspace_firm on matters_workspace (firm_id);
create index if not exists idx_matter_workspace_active on matters_workspace (firm_id, active) where active = true;
create index if not exists idx_matter_workspace_area on matters_workspace (firm_id, area);

-- ============================================================
-- TABLA 4: matter_history (append-only audit por matter)
-- ============================================================
-- Cada evento del matter se registra: gen de doc, edit, nota manual,
-- agent run, MCP query, etc. Sirve para timeline + auditoria.

create table if not exists matter_history (
    id                 uuid primary key default gen_random_uuid(),
    matter_id          uuid not null references matters_workspace(matter_id) on delete cascade,
    firm_id            uuid not null references firms(id) on delete cascade,
    event_type         text not null,               -- 'matter_created', 'document_generated', 'document_edited', 'note_added', 'agent_run', 'mcp_query', 'matter_closed'
    actor_user_id      uuid references users(id),
    actor_agent        text,                        -- 'lean_brain', 'docket_watcher', 'human', etc.
    summary            text not null,               -- one-liner para timeline
    details            jsonb default '{}'::jsonb,
    ref_generation_id  uuid,                        -- FK lazy a generations.id (no constraint para evitar lock)
    ref_skill_command  text,                        -- '/redactar/poder-general' etc.
    ref_document_id    uuid,                        -- FK lazy a document_files
    created_at         timestamptz default now()
);

comment on table matter_history is 'M21.S1: Append-only audit por matter. Trigger UPDATE/DELETE bloqueados via RLS.';

create index if not exists idx_matter_history_matter on matter_history (matter_id, created_at desc);
create index if not exists idx_matter_history_firm on matter_history (firm_id, created_at desc);
create index if not exists idx_matter_history_event on matter_history (matter_id, event_type, created_at desc);

-- ============================================================
-- TABLA 5: firm_seed_documents (docs ejemplares cold-start)
-- ============================================================
-- Documentos firmados que la firma sube en el onboarding para que el sistema
-- aprenda su estilo, posiciones reales en clausulas, voz, etc.

create table if not exists firm_seed_documents (
    id                 uuid primary key default gen_random_uuid(),
    firm_id            uuid not null references firms(id) on delete cascade,
    area               text not null,                -- 'commercial', 'notarial', etc.
    doc_type           text,                         -- 'poder', 'contrato_servicios', 'demanda_laboral', etc.
    filename           text,
    content_bytes      bytea,                        -- archivo binario
    content_text       text,                         -- texto extraido (puede ser null si solo pdf)
    extracted_positions jsonb,                       -- posiciones detectadas: {clauses: [...], style: {...}, deltas: [...]}
    analyzed_at        timestamptz,
    analysis_summary   text,                         -- resumen human-readable del analisis
    uploaded_by_user_id uuid references users(id),
    created_at         timestamptz default now()
);

comment on table firm_seed_documents is 'M21.S1: Documentos ejemplares subidos por la firma en cold-start. Se analizan con LLM para extraer posiciones reales del playbook.';

create index if not exists idx_seed_docs_firm_area on firm_seed_documents (firm_id, area);
create index if not exists idx_seed_docs_analyzed on firm_seed_documents (firm_id, analyzed_at) where analyzed_at is not null;

-- ============================================================
-- TABLA 6: firm_guardrails (config G1-G5 por firm)
-- ============================================================
-- Configuracion de los 5 guardrails universales que aplican en cada SKILL.

create table if not exists firm_guardrails (
    firm_id                          uuid primary key references firms(id) on delete cascade,
    -- G1: Destination check
    destination_check_enabled        boolean default true,
    -- G2: Work-product header
    work_product_header_text         text default 'PRIVILEGIADO Y CONFIDENCIAL — TRABAJO DE ABOGADO',
    work_product_header_enabled      boolean default true,
    -- G3: Non-lawyer gates
    non_lawyer_gates_enabled         boolean default true,
    attorney_contact_user_id         uuid references users(id),
    -- G4: Untrusted data wrapping (siempre on, configurable threshold)
    untrusted_data_wrap_enabled      boolean default true,
    -- G5: Disclosed-document use restrictions (litigation specific)
    disclosed_doc_restrictions_enabled boolean default false,
    -- Reglas de escalation custom
    escalation_rules                 jsonb default '{}'::jsonb,
    -- Notification channels preferidos por firm
    notification_channels            jsonb default '{"email": true, "whatsapp": false, "slack": false}'::jsonb,
    created_at                       timestamptz default now(),
    updated_at                       timestamptz default now()
);

comment on table firm_guardrails is 'M21.S1: Config de los 5 guardrails universales (G1-G5) por firm. Editable desde /v2/firm/guardrails.';

-- ============================================================
-- TABLA 7: cold_start_sessions (state machine onboarding)
-- ============================================================
-- Cada firma hace cold-start UNA vez (puede re-correrlo con --redo).
-- Esta tabla guarda el estado del wizard mientras esta en progreso
-- (permite pausar y resumir).

create table if not exists cold_start_sessions (
    id                 uuid primary key default gen_random_uuid(),
    firm_id            uuid not null references firms(id) on delete cascade,
    area               text not null,                 -- 'commercial', 'notarial', etc. cada area tiene su cold-start propio
    status             text default 'in_progress'     -- 'in_progress', 'completed', 'abandoned'
                       check (status in ('in_progress', 'completed', 'abandoned')),
    current_part       int default 0,                 -- 0..4 (4 partes del wizard)
    answers            jsonb default '{}'::jsonb,     -- respuestas acumuladas por pregunta_id
    seed_docs_count    int default 0,
    voice_transcript_url text,                        -- si fue voice cold-start (M21.S3 voice)
    started_at         timestamptz default now(),
    completed_at       timestamptz,
    updated_at         timestamptz default now()
);

comment on table cold_start_sessions is 'M21.S1: Sesion del wizard de cold-start. Permite pausar/resumir el onboarding.';

create index if not exists idx_cold_start_firm_area on cold_start_sessions (firm_id, area);
create index if not exists idx_cold_start_in_progress on cold_start_sessions (firm_id, area, status) where status = 'in_progress';

-- ============================================================
-- ROW LEVEL SECURITY (RLS) - Multi-tenant isolation
-- ============================================================
-- Cada tabla queda restringida a filas del firm_id del usuario autenticado.
-- Patron usado: auth.uid() resuelve user, users.firm_id da el firm.

-- Helper function (idempotente) para resolver firm_id del user actual
create or replace function current_user_firm_id() returns uuid
language sql security definer stable as $$
    select firm_id from users where id = auth.uid() limit 1;
$$;

-- firms_profile: solo el dueno de la firma puede ver/editar
alter table firms_profile enable row level security;
drop policy if exists firms_profile_select on firms_profile;
create policy firms_profile_select on firms_profile
    for select using (firm_id = current_user_firm_id());
drop policy if exists firms_profile_modify on firms_profile;
create policy firms_profile_modify on firms_profile
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- practice_profile_sections: idem
alter table practice_profile_sections enable row level security;
drop policy if exists practice_profile_sections_select on practice_profile_sections;
create policy practice_profile_sections_select on practice_profile_sections
    for select using (firm_id = current_user_firm_id());
drop policy if exists practice_profile_sections_modify on practice_profile_sections;
create policy practice_profile_sections_modify on practice_profile_sections
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- matters_workspace
alter table matters_workspace enable row level security;
drop policy if exists matters_workspace_select on matters_workspace;
create policy matters_workspace_select on matters_workspace
    for select using (firm_id = current_user_firm_id());
drop policy if exists matters_workspace_modify on matters_workspace;
create policy matters_workspace_modify on matters_workspace
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- matter_history: SELECT por firm, INSERT por firm, NO UPDATE/DELETE (append-only)
alter table matter_history enable row level security;
drop policy if exists matter_history_select on matter_history;
create policy matter_history_select on matter_history
    for select using (firm_id = current_user_firm_id());
drop policy if exists matter_history_insert on matter_history;
create policy matter_history_insert on matter_history
    for insert with check (firm_id = current_user_firm_id());
-- NO policies para UPDATE / DELETE -> queda bloqueado por RLS default-deny

-- firm_seed_documents
alter table firm_seed_documents enable row level security;
drop policy if exists firm_seed_documents_select on firm_seed_documents;
create policy firm_seed_documents_select on firm_seed_documents
    for select using (firm_id = current_user_firm_id());
drop policy if exists firm_seed_documents_modify on firm_seed_documents;
create policy firm_seed_documents_modify on firm_seed_documents
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- firm_guardrails
alter table firm_guardrails enable row level security;
drop policy if exists firm_guardrails_select on firm_guardrails;
create policy firm_guardrails_select on firm_guardrails
    for select using (firm_id = current_user_firm_id());
drop policy if exists firm_guardrails_modify on firm_guardrails;
create policy firm_guardrails_modify on firm_guardrails
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- cold_start_sessions
alter table cold_start_sessions enable row level security;
drop policy if exists cold_start_sessions_select on cold_start_sessions;
create policy cold_start_sessions_select on cold_start_sessions
    for select using (firm_id = current_user_firm_id());
drop policy if exists cold_start_sessions_modify on cold_start_sessions;
create policy cold_start_sessions_modify on cold_start_sessions
    for all using (firm_id = current_user_firm_id()) with check (firm_id = current_user_firm_id());

-- ============================================================
-- VALIDACION POST-MIGRACION
-- ============================================================
do $$
declare
    expected_tables text[] := array[
        'firms_profile',
        'practice_profile_sections',
        'matters_workspace',
        'matter_history',
        'firm_seed_documents',
        'firm_guardrails',
        'cold_start_sessions'
    ];
    t text;
    v_count int;
begin
    foreach t in array expected_tables loop
        select count(*) into v_count
        from information_schema.tables
        where table_schema = 'public' and table_name = t;
        if v_count = 0 then
            raise exception 'M21.S1 validation failed: tabla % NO existe', t;
        end if;
    end loop;
    raise notice 'M21.S1 PASS: 7 tablas creadas correctamente';
end$$;

commit;
