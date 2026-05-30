-- ============================================================
-- LexAI · Sprint M20.11 · FTS index sobre agent_memory
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- agent_memory (creada en M19) ya guarda episodic/semantic memory.
-- Esta migración agrega:
--   1. Columna generada tsvector para FTS rápido (spanish)
--   2. Índice GIN sobre el tsvector
--   3. Helper function lexai_recall_memory(firm_id, query, kind, limit)
--
-- Permite que la tool `recall_memory` haga búsqueda full-text en
-- O(log n) en vez del actual ILIKE O(n).

-- 1. Columna generada (computa el tsvector automáticamente)
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
create or replace function lexai_recall_memory(
  p_firm_id uuid,
  p_query text,
  p_kind text default null,
  p_limit int default 5
)
returns table (
  id uuid,
  kind text,
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
    m.id, m.kind, m.key, m.value,
    ts_rank(m.value_tsv, plainto_tsquery('spanish', p_query)) as rank,
    m.created_at
  from agent_memory m
  where m.firm_id = p_firm_id
    and (p_kind is null or m.kind = p_kind)
    and (m.expires_at is null or m.expires_at > now())
    and m.value_tsv @@ plainto_tsquery('spanish', p_query)
  order by rank desc, m.created_at desc
  limit p_limit;
$$;

grant execute on function lexai_recall_memory(uuid, text, text, int) to authenticated;
grant execute on function lexai_recall_memory(uuid, text, text, int) to service_role;

comment on function lexai_recall_memory(uuid, text, text, int) is
  'M20.11: búsqueda FTS sobre agent_memory por firm_id + query + kind opcional.
   Usado por tool recall_memory (Brain ReAct).';
