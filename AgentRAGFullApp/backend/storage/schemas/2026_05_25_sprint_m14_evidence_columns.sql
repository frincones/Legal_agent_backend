-- ============================================================
-- LexAI · Sprint M14 · Evidence columns + shadow diffs table
-- Migration date: 2026-05-25
-- Idempotent · additive · backward-compatible
-- ============================================================

-- ------------------------------------------------------------
-- 1. citation_verifications: agregar evidence_metadata
-- ------------------------------------------------------------
-- El nuevo VerificationAgent guarda info rica de cada verificación:
-- - tool_chain: orden de tools invocadas
-- - confidence_breakdown: scores por tool
-- - evidence_snippets: extractos relevantes
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'citation_verifications' and column_name = 'evidence_metadata'
  ) then
    alter table citation_verifications add column evidence_metadata jsonb default '{}'::jsonb;
  end if;
end $$;


-- ------------------------------------------------------------
-- 2. verification_attempts: extender con columnas del nuevo agent
-- ------------------------------------------------------------
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'verification_attempts' and column_name = 'confidence_score'
  ) then
    alter table verification_attempts add column confidence_score float;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'verification_attempts' and column_name = 'sources_tried'
  ) then
    alter table verification_attempts add column sources_tried jsonb default '[]'::jsonb;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'verification_attempts' and column_name = 'normalized_ref'
  ) then
    alter table verification_attempts add column normalized_ref text;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'verification_attempts' and column_name = 'tool_results'
  ) then
    alter table verification_attempts add column tool_results jsonb default '[]'::jsonb;
  end if;
end $$;


-- ------------------------------------------------------------
-- 3. verification_attempts: extender check constraint de result_state
-- ------------------------------------------------------------
-- El nuevo agent puede emitir estados adicionales que el legacy no tiene
do $$
declare
  con_name text;
begin
  -- Buscar el constraint existente sobre result_state
  select conname into con_name
  from pg_constraint
  where conrelid = 'verification_attempts'::regclass
    and contype = 'c'
    and pg_get_constraintdef(oid) ilike '%result_state%';

  if con_name is not null then
    execute format('alter table verification_attempts drop constraint %I', con_name);
  end if;

  -- Re-crear con estados extendidos
  alter table verification_attempts
    add constraint verification_attempts_result_state_check
    check (result_state in (
      'verificada', 'no_encontrada', 'sospechosa', 'superada', 'derogada',
      'modulada', 'cache_hit', 'error', 'verified'
    ));
exception
  when others then
    -- Si la tabla no existe o el alter falla, no romper
    raise notice 'verification_attempts constraint update skipped: %', sqlerrm;
end $$;


-- ------------------------------------------------------------
-- 4. verification_shadow_diffs: nueva tabla para shadow mode
-- ------------------------------------------------------------
-- Cuando USE_VERIFICATION_AGENT=1 + SHADOW_MODE=1, ambos paths corren en
-- paralelo. Esta tabla audita las divergencias para comparar antes del
-- flip definitivo en M15.
create table if not exists verification_shadow_diffs (
  id                uuid primary key default gen_random_uuid(),
  generation_id    uuid,
  citation_ref     text not null,
  citation_type    text,
  legacy_state     text,
  legacy_method    text,
  legacy_fuente_url text,
  agent_state      text,
  agent_method     text,
  agent_confidence float,
  agent_fuente_url text,
  agent_sources_tried jsonb default '[]'::jsonb,
  is_critical      boolean default false,  -- true si legacy=verificada Y agent=no_encontrada (o viceversa)
  diff_type        text,                    -- 'critical' | 'medium' | 'minor' | 'identical'
  created_at       timestamptz not null default now()
);

create index if not exists verification_shadow_diffs_critical_idx
  on verification_shadow_diffs (is_critical, created_at desc)
  where is_critical = true;
create index if not exists verification_shadow_diffs_recent_idx
  on verification_shadow_diffs (created_at desc);
create index if not exists verification_shadow_diffs_citation_idx
  on verification_shadow_diffs (citation_ref);

alter table verification_shadow_diffs enable row level security;

drop policy if exists verification_shadow_diffs_select on verification_shadow_diffs;
create policy verification_shadow_diffs_select on verification_shadow_diffs for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists verification_shadow_diffs_modify on verification_shadow_diffs;
create policy verification_shadow_diffs_modify on verification_shadow_diffs for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ============================================================
-- FIN Sprint M14 migration
-- ============================================================
