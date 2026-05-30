-- ============================================================
-- LexAI · Sprint M20.11 · FTS index sobre agent_memory
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- agent_memory tiene schema: id, firm_id, user_id, scope, scope_ref,
-- key, value (jsonb), embedding, ttl_until, created_at, updated_at.
-- Agregamos FTS rápido sobre key+value::text para que la tool
-- recall_memory haga búsqueda O(log n) en vez de ILIKE O(n).

-- 1. Columna generada (tsvector spanish sobre key + value)
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'agent_memory' and column_name = 'value_tsv'
  ) then
    alter table agent_memory
      add column value_tsv tsvector
      generated always as (
        to_tsvector(
          'spanish',
          coalesce(key, '') || ' ' || coalesce(value::text, '')
        )
      ) stored;
  end if;
end$$;

-- 2. Índice GIN
create index if not exists agent_memory_value_tsv_idx
  on agent_memory using gin (value_tsv);

-- 3. Función de búsqueda
-- Nota: agent_memory usa `scope` (no `kind`) y `ttl_until` (no `expires_at`).
-- El parámetro p_kind se mantiene por compat con la tool recall_memory,
-- mapeándose a `scope`.
create or replace function lexai_recall_memory(
  p_firm_id uuid,
  p_query text,
  p_kind text default null,
  p_limit int default 5
)
returns table (
  id uuid,
  scope text,
  key text,
  value jsonb,
  rank real,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
  select
    m.id, m.scope, m.key, m.value,
    ts_rank(m.value_tsv, plainto_tsquery('spanish', p_query)) as rank,
    m.created_at
  from agent_memory m
  where m.firm_id = p_firm_id
    and (p_kind is null or m.scope = p_kind)
    and (m.ttl_until is null or m.ttl_until > now())
    and m.value_tsv @@ plainto_tsquery('spanish', p_query)
  order by rank desc, m.created_at desc
  limit p_limit;
$$;

grant execute on function lexai_recall_memory(uuid, text, text, int) to authenticated;
grant execute on function lexai_recall_memory(uuid, text, text, int) to service_role;

comment on function lexai_recall_memory(uuid, text, text, int) is
  'M20.11: búsqueda FTS sobre agent_memory por firm_id + query + scope opcional.
   Usado por tool recall_memory (Brain ReAct).';
