-- ============================================================
-- LexAI · F5 · Persistent agent memory
-- Migration date: 2026-05-03
-- Idempotent · additive
-- ============================================================

create table if not exists agent_memory (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid references users(id) on delete cascade,
  scope           text not null default 'firm',  -- 'firm' | 'user' | 'matter'
  scope_ref       uuid,                           -- matter_id si scope='matter'
  key             text not null,
  value           jsonb not null,
  embedding       vector(1536),                   -- para recall_relevant
  ttl_until       timestamptz,                    -- null = permanente
  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),
  unique (firm_id, scope, scope_ref, key)
);

create index if not exists agent_memory_firm_scope_idx
  on agent_memory (firm_id, scope, scope_ref);
create index if not exists agent_memory_ttl_idx
  on agent_memory (ttl_until) where ttl_until is not null;

-- Vector index para recall_relevant (HNSW si pgvector >= 0.5)
do $$
begin
  if exists (select 1 from pg_extension where extname = 'vector') then
    begin
      create index if not exists agent_memory_embed_idx
        on agent_memory using hnsw (embedding vector_cosine_ops);
    exception when others then
      -- HNSW no disponible (pgvector < 0.5); fallback a IVFFlat
      create index if not exists agent_memory_embed_idx
        on agent_memory using ivfflat (embedding vector_cosine_ops) with (lists = 50);
    end;
  end if;
end $$;

alter table agent_memory enable row level security;

drop policy if exists agent_memory_select on agent_memory;
drop policy if exists agent_memory_modify on agent_memory;
create policy agent_memory_select on agent_memory for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy agent_memory_modify on agent_memory for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop trigger if exists trg_agent_memory_firm_id on agent_memory;
create trigger trg_agent_memory_firm_id
  before insert on agent_memory
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_agent_memory_updated_at on agent_memory;
create trigger trg_agent_memory_updated_at
  before update on agent_memory
  for each row execute function tg_set_updated_at();

-- Helper RPC: limpia TTL expirado (puede llamarse desde cron)
create or replace function lexai_memory_purge_expired()
returns int
language sql
as $$
  with deleted as (
    delete from agent_memory
    where ttl_until is not null and ttl_until < now()
    returning 1
  )
  select count(*)::int from deleted;
$$;
