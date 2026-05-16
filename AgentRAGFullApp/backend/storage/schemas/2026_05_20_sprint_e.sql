-- ================================================================
-- Sprint E · Drafting & Review igualar Claude for Legal
-- ================================================================
-- ALCANCE: 5 tablas (firm_playbook, firm_skills, canvas_redlines,
-- skill_executions, clause_review_results) + RPCs + RLS + Realtime
-- + añadir 4 modules al catálogo de entitlements.
--
-- ADITIVO: NO toca tablas existentes (canvas, matter_documents,
-- contract_*, doc_compare_*). Solo añade nuevas.
-- ================================================================

-- ----------------------------------------------------------------
-- 1. firm_playbook · CLAUDE.md por firma (configurable)
-- ----------------------------------------------------------------

create table if not exists firm_playbook (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade unique,

  -- Preferencias semanticas
  jurisdiction_default text default 'co'
    check (jurisdiction_default in ('co','mx','pe','cl','ar','us','other')),
  redline_style text default 'tracked'
    check (redline_style in ('tracked','inline','sidebar')),
  tone text default 'formal'
    check (tone in ('formal','neutral','aggressive')),

  -- Reglas estructuradas
  preferred_clauses jsonb default '{}'::jsonb,    -- {clause_type: "texto preferido"}
  forbidden_terms text[] default '{}',            -- términos que bloquean redline
  required_clauses text[] default '{}',           -- cláusulas obligatorias en contratos
  escalation_matrix jsonb default '[]'::jsonb,    -- [{rango_cop: [...], aprobador: "..."}]

  -- Raw markdown (admin edita libre)
  raw_md text,

  -- Versioning
  version int default 1,
  updated_by uuid,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create or replace function lexai_firm_playbook_touch()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  new.version = coalesce(old.version, 0) + 1;
  return new;
end;
$$;

drop trigger if exists firm_playbook_touch on firm_playbook;
create trigger firm_playbook_touch before update on firm_playbook
  for each row execute function lexai_firm_playbook_touch();

alter table firm_playbook enable row level security;
drop policy if exists fp_select on firm_playbook;
drop policy if exists fp_modify on firm_playbook;
create policy fp_select on firm_playbook for select using (firm_id = auth_firm_id());
create policy fp_modify on firm_playbook for all using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 2. firm_skills · registry de skills (builtin global + custom por firma)
-- ----------------------------------------------------------------

create table if not exists firm_skills (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid references firms(id) on delete cascade,  -- null = builtin global
  command text not null,                          -- ej. "/revisar/contrato"
  name text not null,
  description text,
  category text,                                  -- drafting | review | analysis | other
  frontmatter jsonb default '{}'::jsonb,          -- YAML parsed: argument-hint, allowed-tools, etc
  system_prompt text not null,
  output_schema jsonb,                            -- JSON schema para structured output
  references_md text,                             -- markdown adicional (legal context)
  user_invocable boolean default true,
  jurisdiction text default 'co',
  version int default 1,
  status text default 'published'
    check (status in ('draft','published','deprecated','archived')),
  metadata jsonb default '{}'::jsonb,
  created_by uuid,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (firm_id, command, version)
);

create index if not exists firm_skills_command_idx
  on firm_skills (command) where status = 'published';
create index if not exists firm_skills_firm_idx
  on firm_skills (firm_id, status) where firm_id is not null;
create index if not exists firm_skills_builtin_idx
  on firm_skills (command, status) where firm_id is null;

drop trigger if exists firm_skills_touch on firm_skills;
create trigger firm_skills_touch before update on firm_skills
  for each row execute function lexai_firm_integrations_touch();

alter table firm_skills enable row level security;
drop policy if exists fs_select on firm_skills;
drop policy if exists fs_modify on firm_skills;
-- builtin (firm_id=null) son visibles para todos los authenticated
create policy fs_select on firm_skills for select
  using (firm_id is null or firm_id = auth_firm_id());
-- modificar SOLO las propias de la firma (builtin las gestiona admin SaaS via service_role)
create policy fs_modify on firm_skills for all
  using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 3. canvas_redlines · redlines persistentes (pending/applied/rejected)
-- ----------------------------------------------------------------

create table if not exists canvas_redlines (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  matter_id uuid references matters(id) on delete set null,
  document_id uuid,                               -- matter_documents.id si aplica
  canvas_session_id uuid,                         -- correlación con sesión canvas
  source_skill_id uuid references firm_skills(id) on delete set null,

  redlines jsonb not null,                        -- array de redlines del schema
  original_text text,                             -- snapshot para auditoria
  result_text text,                               -- aplicaciones finalizadas

  status text not null default 'pending'
    check (status in ('pending','applied','rejected','superseded')),
  applied_count int default 0,
  rejected_count int default 0,

  created_by uuid,
  applied_by uuid,
  applied_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists canvas_redlines_matter_idx
  on canvas_redlines (firm_id, matter_id, status);
create index if not exists canvas_redlines_doc_idx
  on canvas_redlines (document_id, status)
  where document_id is not null;

drop trigger if exists canvas_redlines_touch on canvas_redlines;
create trigger canvas_redlines_touch before update on canvas_redlines
  for each row execute function lexai_firm_integrations_touch();

alter table canvas_redlines enable row level security;
drop policy if exists cr_select on canvas_redlines;
drop policy if exists cr_modify on canvas_redlines;
create policy cr_select on canvas_redlines for select using (firm_id = auth_firm_id());
create policy cr_modify on canvas_redlines for all using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 4. skill_executions · audit trail
-- ----------------------------------------------------------------

create table if not exists skill_executions (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  user_id uuid not null,
  skill_id uuid references firm_skills(id) on delete set null,
  command text not null,
  matter_id uuid,
  document_id uuid,

  input_summary jsonb default '{}'::jsonb,        -- args sin PII
  output_summary jsonb default '{}'::jsonb,
  status text not null default 'running'
    check (status in ('running','success','error','blocked_by_hook','quota_exceeded','timeout')),
  error_message text,
  hooks_fired text[] default '{}',
  duration_ms int,
  tokens_input int,
  tokens_output int,
  cost_usd_cents int,

  started_at timestamptz default now(),
  completed_at timestamptz
);

create index if not exists skill_executions_firm_idx
  on skill_executions (firm_id, started_at desc);
create index if not exists skill_executions_user_idx
  on skill_executions (user_id, started_at desc);
create index if not exists skill_executions_command_idx
  on skill_executions (command, status, started_at desc);

alter table skill_executions enable row level security;
drop policy if exists se_select on skill_executions;
drop policy if exists se_modify on skill_executions;
create policy se_select on skill_executions for select using (firm_id = auth_firm_id());
create policy se_modify on skill_executions for all using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 5. clause_review_results · cache de revisión cláusula-por-cláusula
-- ----------------------------------------------------------------

create table if not exists clause_review_results (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid not null references firms(id) on delete cascade,
  matter_document_id uuid not null,               -- matter_documents.id
  matter_id uuid,
  skill_id uuid references firm_skills(id) on delete set null,

  clause_index int,
  clause_title text,
  clause_text text,
  category text,                                  -- objeto, valor, indemnizacion, ...

  severity text not null default 'green'
    check (severity in ('green','yellow','red','info')),
  reason text,
  suggested_text text,
  suggested_action text,
  citations jsonb default '[]'::jsonb,
  metadata jsonb default '{}'::jsonb,

  status text default 'open'
    check (status in ('open','accepted','dismissed','superseded')),

  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists clause_review_doc_idx
  on clause_review_results (matter_document_id, status);
create index if not exists clause_review_firm_idx
  on clause_review_results (firm_id, severity, status);

drop trigger if exists clause_review_touch on clause_review_results;
create trigger clause_review_touch before update on clause_review_results
  for each row execute function lexai_firm_integrations_touch();

alter table clause_review_results enable row level security;
drop policy if exists crr_select on clause_review_results;
drop policy if exists crr_modify on clause_review_results;
create policy crr_select on clause_review_results for select using (firm_id = auth_firm_id());
create policy crr_modify on clause_review_results for all using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 6. skill_hooks · hooks habilitables por admin SaaS
-- ----------------------------------------------------------------

create table if not exists skill_hooks (
  id uuid primary key default gen_random_uuid(),
  hook_key text not null unique,
  name text not null,
  description text,
  hook_type text not null
    check (hook_type in ('pre_skill','post_skill','pre_redline_apply','post_redline_apply')),
  python_module text not null,                    -- ej. 'hooks.habeas_data_linter'
  function_name text default 'run',
  applies_to text[] default '{}',                 -- skills donde aplica · null/[] = todas
  enabled boolean default true,
  order_index int default 100,
  decision_mode text default 'block'
    check (decision_mode in ('block','warn','log')),
  config jsonb default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

-- No tiene firm_id · es global · solo service_role escribe via /v1/admin/saas/hooks
alter table skill_hooks enable row level security;
drop policy if exists sh_select on skill_hooks;
create policy sh_select on skill_hooks for select using (true);

-- ----------------------------------------------------------------
-- 7. RPC helpers
-- ----------------------------------------------------------------

create or replace function lexai_get_active_skills(p_firm_id uuid)
returns table (
  id uuid,
  command text,
  name text,
  description text,
  category text,
  jurisdiction text,
  user_invocable boolean,
  frontmatter jsonb,
  is_custom boolean
)
language sql stable security definer as $$
  -- Skills custom de la firma (override builtin) + builtins no-override
  with firm_commands as (
    select command from firm_skills
     where firm_id = p_firm_id and status = 'published'
  )
  select id, command, name, description, category, jurisdiction,
         user_invocable, frontmatter, true as is_custom
    from firm_skills
   where firm_id = p_firm_id and status = 'published'
  union all
  select id, command, name, description, category, jurisdiction,
         user_invocable, frontmatter, false as is_custom
    from firm_skills
   where firm_id is null and status = 'published'
     and command not in (select command from firm_commands)
   order by category, command;
$$;
grant execute on function lexai_get_active_skills(uuid) to authenticated, service_role;

create or replace function lexai_resolve_skill(p_firm_id uuid, p_command text)
returns table (
  id uuid,
  command text,
  name text,
  system_prompt text,
  output_schema jsonb,
  references_md text,
  frontmatter jsonb,
  is_custom boolean
)
language sql stable security definer as $$
  -- Primero busca custom de la firma, fallback a builtin
  (select id, command, name, system_prompt, output_schema,
          references_md, frontmatter, true as is_custom
     from firm_skills
    where firm_id = p_firm_id and command = p_command and status = 'published'
    order by version desc limit 1)
  union all
  (select id, command, name, system_prompt, output_schema,
          references_md, frontmatter, false as is_custom
     from firm_skills
    where firm_id is null and command = p_command and status = 'published'
    order by version desc limit 1)
  limit 1;
$$;
grant execute on function lexai_resolve_skill(uuid, text) to authenticated, service_role;

create or replace function lexai_get_active_hooks(p_skill_command text default null, p_type text default null)
returns table (
  id uuid,
  hook_key text,
  hook_type text,
  python_module text,
  function_name text,
  decision_mode text,
  order_index int,
  config jsonb
)
language sql stable security definer as $$
  select id, hook_key, hook_type, python_module, function_name,
         decision_mode, order_index, config
    from skill_hooks
   where enabled = true
     and (p_type is null or hook_type = p_type)
     and (p_skill_command is null
          or applies_to = '{}'
          or p_skill_command = any(applies_to))
   order by order_index asc;
$$;
grant execute on function lexai_get_active_hooks(text, text) to authenticated, service_role;

-- ----------------------------------------------------------------
-- 8. Realtime publications
-- ----------------------------------------------------------------

do $$
declare
  v_t text;
begin
  for v_t in select unnest(array['canvas_redlines','clause_review_results',
                                  'firm_playbook','firm_skills',
                                  'skill_executions']) loop
    if not exists (
      select 1 from pg_publication_tables
       where pubname='supabase_realtime' and tablename=v_t
    ) then
      execute format('alter publication supabase_realtime add table %I', v_t);
    end if;
  end loop;
end $$;

-- ----------------------------------------------------------------
-- 9. Seed inicial · hooks default
-- ----------------------------------------------------------------

insert into skill_hooks (hook_key, name, description, hook_type, python_module, decision_mode, order_index, applies_to)
values
  ('habeas_data_linter', 'Habeas Data linter (Ley 1581)',
   'Verifica cláusula Ley 1581 obligatoria en contratos con datos personales',
   'pre_redline_apply', 'hooks.habeas_data_linter', 'warn', 10,
   array['/revisar/contrato','/revisar/dpa-habeas-data','/redactar/contrato']),
  ('citation_verifier', 'Verificador de citas T-XXX',
   'Valida citas T-XXX/AAAA contra tabla jurisprudencia',
   'post_skill', 'hooks.citation_verifier', 'warn', 20,
   array['/redactar/tutela','/redactar/derecho-peticion']),
  ('forbidden_terms_blocker', 'Bloqueador de términos prohibidos',
   'Bloquea redlines con términos del playbook firm.forbidden_terms',
   'pre_redline_apply', 'hooks.forbidden_terms_blocker', 'block', 30, '{}'),
  ('required_clauses_checker', 'Verificador de cláusulas obligatorias',
   'Valida required_clauses presentes en contratos',
   'post_skill', 'hooks.required_clauses_checker', 'warn', 40,
   array['/revisar/contrato','/redactar/contrato']),
  ('clause_severity_classifier', 'Clasificador severity GREEN/YELLOW/RED',
   'LLM para clasificar severity con playbook context',
   'post_skill', 'hooks.clause_severity_classifier', 'log', 50,
   array['/revisar/contrato','/revisar/dpa-habeas-data'])
on conflict (hook_key) do nothing;

-- ----------------------------------------------------------------
-- 10. Verificación
-- ----------------------------------------------------------------

select
  'firm_playbook' as tabla,
  (select count(*) from pg_policies where tablename='firm_playbook') as policies,
  (select count(*) from pg_indexes where tablename='firm_playbook') as indexes
union all select 'firm_skills',
  (select count(*) from pg_policies where tablename='firm_skills'),
  (select count(*) from pg_indexes where tablename='firm_skills')
union all select 'canvas_redlines',
  (select count(*) from pg_policies where tablename='canvas_redlines'),
  (select count(*) from pg_indexes where tablename='canvas_redlines')
union all select 'skill_executions',
  (select count(*) from pg_policies where tablename='skill_executions'),
  (select count(*) from pg_indexes where tablename='skill_executions')
union all select 'clause_review_results',
  (select count(*) from pg_policies where tablename='clause_review_results'),
  (select count(*) from pg_indexes where tablename='clause_review_results')
union all select 'skill_hooks',
  (select count(*) from pg_policies where tablename='skill_hooks'),
  (select count(*) from pg_indexes where tablename='skill_hooks')
union all select 'rpc lexai_get_active_skills',
  (select count(*) from pg_proc where proname='lexai_get_active_skills'),
  null
union all select 'rpc lexai_resolve_skill',
  (select count(*) from pg_proc where proname='lexai_resolve_skill'),
  null
union all select 'rpc lexai_get_active_hooks',
  (select count(*) from pg_proc where proname='lexai_get_active_hooks'),
  null
union all select 'realtime publications',
  (select count(*) from pg_publication_tables
    where pubname='supabase_realtime'
      and tablename in ('canvas_redlines','clause_review_results',
                         'firm_playbook','firm_skills','skill_executions')),
  null
union all select 'seed hooks',
  (select count(*) from skill_hooks),
  null;
