-- ============================================================================
-- Sprint M19.25 + M19.26 · Marketplace tier + Claude renderer audit
-- ============================================================================
-- Aditiva. Idempotente. Reusa firm_skills, skill_executions, output_styles.
-- Nuevas tablas: skill_assets, skill_learning_jobs, claude_render_audit.
-- Cambios firm_skills: tier column + index default_scope.
-- ============================================================================

begin;

-- ----------------------------------------------------------------------------
-- 1. firm_skills · tier (public | premium) + default_scope index
-- ----------------------------------------------------------------------------

alter table firm_skills
  add column if not exists tier text default 'public';

-- CHECK constraint solo si no existe ya
do $$
begin
  if not exists (
    select 1 from information_schema.check_constraints
    where constraint_name = 'firm_skills_tier_check'
  ) then
    alter table firm_skills
      add constraint firm_skills_tier_check
      check (tier in ('public','premium'));
  end if;
end$$;

create index if not exists firm_skills_tier_status_idx
  on firm_skills (tier, status)
  where firm_id is null;

-- Frontmatter expone default_scope: doc_type | doc_family | global | matter
create index if not exists firm_skills_default_scope_idx
  on firm_skills ((frontmatter->>'default_scope'));

-- Frontmatter expone doc_type para selector LLM (rápido por nombre)
create index if not exists firm_skills_doc_type_idx
  on firm_skills ((frontmatter->>'doc_type'));

-- ----------------------------------------------------------------------------
-- 2. skill_assets · adjuntos (.docx examples, validation, bundled)
-- ----------------------------------------------------------------------------
-- Patrón Anthropic Skills: progressive disclosure. Esta tabla apunta a
-- objetos en Supabase Storage (bucket 'skill-assets'). El loader descarga
-- el SKILL.md primero; los assets solo cuando el agente los necesita.

create table if not exists skill_assets (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid references firms(id) on delete cascade,  -- null = builtin global
  skill_id uuid not null references firm_skills(id) on delete cascade,

  kind text not null
    check (kind in ('example_docx','validation_template','bundled_script','reference_pdf','reference_md')),
  storage_bucket text not null default 'skill-assets',
  storage_path text not null,
  filename text not null,
  size_bytes int,
  mime_type text,
  sha256 text,

  -- Metadata para progressive disclosure
  load_priority int default 0,             -- 0=lazy, 1=eager, 2=metadata-only
  description text,
  metadata jsonb default '{}'::jsonb,

  created_by uuid,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (skill_id, kind, filename)
);

create index if not exists skill_assets_skill_kind_idx
  on skill_assets (skill_id, kind);
create index if not exists skill_assets_firm_idx
  on skill_assets (firm_id) where firm_id is not null;

alter table skill_assets enable row level security;
drop policy if exists sa_select on skill_assets;
drop policy if exists sa_modify on skill_assets;
create policy sa_select on skill_assets for select
  using (firm_id is null or firm_id = auth_firm_id());
create policy sa_modify on skill_assets for all
  using (firm_id is not null and firm_id = auth_firm_id());

-- ----------------------------------------------------------------------------
-- 3. skill_learning_jobs · async "Learn from docs" pipeline
-- ----------------------------------------------------------------------------
-- El usuario sube N .docx ejemplo y un agente infiere SKILL.md (frontmatter,
-- system_prompt, references_md). Cuando aprueba → se materializa en firm_skills.

create table if not exists skill_learning_jobs (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  user_id uuid not null,

  -- Inputs
  source_documents jsonb not null,        -- [{bucket, path, filename, mime, sha256}]
  target_skill_command text,              -- ej. "/redactar/contrato-firma-acme"
  hint_doc_type text,                     -- pista del usuario

  -- Outputs inferidos (LLM)
  inferred_frontmatter jsonb,
  inferred_system_prompt text,
  inferred_references_md text,
  inferred_output_schema jsonb,
  inferred_confidence numeric(4,2),       -- 0-1

  -- Estado del pipeline
  status text not null default 'queued'
    check (status in ('queued','running','succeeded','failed','approved','rejected','expired')),
  error_message text,
  materialized_skill_id uuid references firm_skills(id) on delete set null,

  -- Métricas
  duration_ms int,
  tokens_input int,
  tokens_output int,
  cost_usd_cents int,

  created_at timestamptz default now(),
  started_at timestamptz,
  completed_at timestamptz,
  approved_by uuid,
  approved_at timestamptz
);

create index if not exists slj_firm_status_idx
  on skill_learning_jobs (firm_id, status, created_at desc);
create index if not exists slj_queue_idx
  on skill_learning_jobs (status, created_at)
  where status in ('queued','running');

alter table skill_learning_jobs enable row level security;
drop policy if exists slj_all on skill_learning_jobs;
create policy slj_all on skill_learning_jobs for all
  using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------------------
-- 4. claude_render_audit · auditoría del renderer camino B
-- ----------------------------------------------------------------------------
-- Cada vez que claude_docx_renderer genera un .docx: registra prompt, JS
-- generado, status (success | js_error | timeout | fallback), duración,
-- tokens, costo. Permite debugging y mejora del prompt del renderer.

create table if not exists claude_render_audit (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid references firms(id) on delete cascade,
  user_id uuid,
  matter_id uuid,
  document_id uuid,
  skill_id uuid references firm_skills(id) on delete set null,

  -- Input
  doc_type text,
  prompt_summary text,                    -- prompt sin PII (truncado)
  prompt_tokens int,
  user_payload_summary jsonb,             -- datos del formulario sin PII

  -- LLM call
  llm_model text default 'claude-opus-4-7',
  llm_temperature numeric(3,2),
  retry_count int default 0,

  -- Output
  generated_js text,                      -- código JS para docx-js
  generated_js_sha256 text,
  generated_js_bytes int,
  output_docx_bytes int,
  output_docx_sha256 text,

  -- Status
  status text not null default 'running'
    check (status in ('running','success','js_error','sandbox_timeout','sandbox_oom','llm_error','fallback_legacy','rejected_by_validator')),
  error_message text,
  fallback_used boolean default false,
  fallback_reason text,

  -- Métricas
  duration_ms int,
  llm_duration_ms int,
  sandbox_duration_ms int,
  tokens_input int,
  tokens_output int,
  cost_usd_cents int,

  created_at timestamptz default now(),
  completed_at timestamptz
);

create index if not exists cra_firm_created_idx
  on claude_render_audit (firm_id, created_at desc);
create index if not exists cra_status_idx
  on claude_render_audit (status, created_at desc);
create index if not exists cra_doc_type_idx
  on claude_render_audit (doc_type, status);

alter table claude_render_audit enable row level security;
drop policy if exists cra_select on claude_render_audit;
drop policy if exists cra_insert on claude_render_audit;
create policy cra_select on claude_render_audit for select
  using (firm_id is null or firm_id = auth_firm_id());
-- Inserción solo via service_role (backend), no via RLS de usuario
create policy cra_insert on claude_render_audit for insert
  with check (true);

-- ----------------------------------------------------------------------------
-- 5. Verificación final
-- ----------------------------------------------------------------------------

do $$
declare
  v_tier_col_exists bool;
  v_skill_assets_exists bool;
  v_slj_exists bool;
  v_cra_exists bool;
begin
  select exists(
    select 1 from information_schema.columns
    where table_name='firm_skills' and column_name='tier'
  ) into v_tier_col_exists;

  select exists(select 1 from information_schema.tables where table_name='skill_assets')
    into v_skill_assets_exists;
  select exists(select 1 from information_schema.tables where table_name='skill_learning_jobs')
    into v_slj_exists;
  select exists(select 1 from information_schema.tables where table_name='claude_render_audit')
    into v_cra_exists;

  if not v_tier_col_exists then raise exception 'firm_skills.tier MISSING'; end if;
  if not v_skill_assets_exists then raise exception 'skill_assets MISSING'; end if;
  if not v_slj_exists then raise exception 'skill_learning_jobs MISSING'; end if;
  if not v_cra_exists then raise exception 'claude_render_audit MISSING'; end if;

  raise notice 'Sprint M19.25+M19.26 migration OK';
end$$;

commit;
