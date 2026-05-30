-- ============================================================
-- LexAI · Sprint M20.01 · tool_call_audit
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Audit granular de cada invocación de tool dentro del nuevo
-- LeanOrchestrator (ReAct loop). Permite trazabilidad por
-- generation_id, performance por tool, cache hit rate, y
-- detección de regresiones.
--
-- No reemplaza generation_audit (que sigue siendo el audit de
-- alto nivel por documento); este registra cada llamada
-- atómica a una tool dentro de la generación.

create table if not exists tool_call_audit (
  id              uuid primary key default gen_random_uuid(),
  generation_id   uuid not null,                          -- enlaza con generation_audit
  firm_id         uuid,                                    -- soft FK firms (multi-tenant)
  user_id         uuid,                                    -- soft FK users
  tool_name       text not null,                           -- 'verify_citation', 'generate_clause', etc.
  iteration       int not null default 0,                  -- # de iteración del ReAct loop
  started_at      timestamptz not null default now(),
  duration_ms     int,                                     -- null si aún en curso
  input_hash      text,                                    -- sha256 del input (para deduplicación cache)
  output_hash     text,                                    -- sha256 del output
  success         boolean,                                 -- true|false|null (en curso)
  error_class     text,                                    -- exception class si falló
  error_message   text,
  cached          boolean not null default false,          -- true si retornó de cache (Redis/SQL)
  model_used      text,                                    -- 'claude-sonnet-4-6'|'claude-opus-4-7'|'gpt-4o' si la tool invocó LLM
  tokens_in       int,                                     -- si la tool invocó LLM
  tokens_out      int,
  cost_usd        numeric(10,6),                           -- si la tool invocó LLM
  cache_creation_tokens int default 0,                     -- prompt caching Anthropic
  cache_read_tokens     int default 0,                     -- prompt caching Anthropic
  metadata        jsonb not null default '{}'::jsonb,      -- payload adicional libre
  created_at      timestamptz not null default now()
);

-- ---- índices para queries comunes ----
create index if not exists tool_call_audit_generation_idx
  on tool_call_audit (generation_id, iteration);
create index if not exists tool_call_audit_firm_started_idx
  on tool_call_audit (firm_id, started_at desc);
create index if not exists tool_call_audit_tool_success_idx
  on tool_call_audit (tool_name, success);
create index if not exists tool_call_audit_firm_tool_idx
  on tool_call_audit (firm_id, tool_name, started_at desc);

-- ---- columnas adicionales si la tabla ya existía (idempotente) ----
do $$
begin
  if not exists (select 1 from information_schema.columns
    where table_name='tool_call_audit' and column_name='cache_creation_tokens') then
    alter table tool_call_audit add column cache_creation_tokens int default 0;
  end if;
  if not exists (select 1 from information_schema.columns
    where table_name='tool_call_audit' and column_name='cache_read_tokens') then
    alter table tool_call_audit add column cache_read_tokens int default 0;
  end if;
  if not exists (select 1 from information_schema.columns
    where table_name='tool_call_audit' and column_name='iteration') then
    alter table tool_call_audit add column iteration int not null default 0;
  end if;
end$$;

-- ---- RLS multi-tenant (mismo patrón que generation_audit) ----
alter table tool_call_audit enable row level security;

drop policy if exists tool_call_audit_select on tool_call_audit;
create policy tool_call_audit_select on tool_call_audit for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists tool_call_audit_modify on tool_call_audit;
create policy tool_call_audit_modify on tool_call_audit for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ---- columna nueva en generation_audit para prompt caching agregado ----
do $$
begin
  if not exists (select 1 from information_schema.columns
    where table_name='generation_audit' and column_name='cache_hit_tokens') then
    alter table generation_audit add column cache_hit_tokens int default 0;
  end if;
  if not exists (select 1 from information_schema.columns
    where table_name='generation_audit' and column_name='orchestrator_kind') then
    alter table generation_audit add column orchestrator_kind text default 'legacy'
      check (orchestrator_kind in ('legacy', 'lean'));
  end if;
end$$;

comment on table tool_call_audit is
  'M20.01: audit granular por tool dentro del LeanOrchestrator (ReAct loop).
   Permite trazabilidad, performance por tool y cache hit rate. No reemplaza
   generation_audit; lo complementa con el detalle de cada tool call.';

comment on column generation_audit.cache_hit_tokens is
  'M20.01: tokens leídos desde prompt cache de Anthropic (suma de todas las iteraciones).';

comment on column generation_audit.orchestrator_kind is
  'M20.01: identifica si la generación usó el orchestrator legacy (17 stages) o el nuevo lean (ReAct + 18 tools).';
