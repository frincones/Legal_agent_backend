-- ============================================================
-- LexAI · F2 · External fetch cache
-- Migration date: 2026-05-03
-- Idempotent · additive
-- ============================================================

-- Cache global de fetches a portales externos (no scoped por firm).
-- Las consultas a SUIN/RUE/DOF/Banrep son determinísticas, así que
-- podemos compartir cache entre firms para bajar carga al portal.

create table if not exists external_fetch_cache (
  cache_key       text primary key,             -- 'suin:ley:1581:2012' | 'banrep:dtf:2026-05'
  source          text not null,                -- 'suin' | 'rue' | 'dof' | 'banrep' | 'generic'
  url             text,
  content_jsonb   jsonb,                        -- payload normalizado
  content_text    text,                         -- texto crudo (HTML/JSON pre-parse)
  status          text not null default 'ok',   -- 'ok' | 'error' | 'not_found'
  error_message   text,
  fetched_at      timestamptz default now(),
  ttl_seconds     int not null default 86400,
  hit_count       int not null default 0
);

create index if not exists external_cache_source_idx
  on external_fetch_cache (source, fetched_at desc);

-- RLS: público-read para authenticated, sólo service writes.
alter table external_fetch_cache enable row level security;

drop policy if exists external_cache_select on external_fetch_cache;
drop policy if exists external_cache_modify on external_fetch_cache;
create policy external_cache_select on external_fetch_cache for select
  using (auth.role() in ('authenticated', 'service_role'));
create policy external_cache_modify on external_fetch_cache for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- RPC para resolver cache con TTL check
create or replace function lexai_cache_get(
  p_key text,
  p_max_age_seconds int default null
) returns jsonb
language sql
stable
as $$
  select case
    when fetched_at + (
      coalesce(p_max_age_seconds, ttl_seconds) || ' seconds'
    )::interval < now() then null
    else jsonb_build_object(
      'cache_key', cache_key,
      'source', source,
      'url', url,
      'content_jsonb', content_jsonb,
      'content_text', content_text,
      'status', status,
      'fetched_at', fetched_at,
      'cached', true
    )
  end
  from external_fetch_cache
  where cache_key = p_key
  limit 1;
$$;
