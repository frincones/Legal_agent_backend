-- ============================================================
-- LexAI · Sprint M19.5 · Chat messages persistence (Claude-style)
-- Migration date: 2026-05-27
-- Idempotent · additive · RLS por firm_id
-- ============================================================
--
-- Persiste los mensajes del agente (con prosa + tool calls trazables)
-- por documento generado, para que el usuario pueda volver y ver el
-- thread completo igual que en Claude.ai.
--
-- Granularidad: 1 row por "thread" del agente (un thread = un mensaje
-- compuesto del asistente con segments de prosa + tool calls).
-- Los segments se guardan en JSONB ordenados temporalmente.

create table if not exists chat_messages (
  id                  uuid primary key default gen_random_uuid(),
  thread_id           text not null,             -- viene del backend (orchestrator)

  -- Relaciones (opcionales)
  firm_id             uuid references firms(id) on delete cascade,
  user_id             uuid,
  generation_id       text,                       -- generation_id del orchestrator
  document_id         uuid,                       -- matter_document si aplica
  matter_id           uuid,

  -- Contenido
  role                text not null check (role in ('user', 'assistant', 'system', 'tool')),
  channel             text not null default 'chat' check (channel in ('voice', 'chat', 'composer')),

  -- Para role='user': content es el prompt original
  -- Para role='assistant': content es el resumen (primer paragraph), segments tiene todo
  content             text,

  -- M19.5: segments ordenados (paragraphs + tools)
  -- [
  --   {"type":"paragraph","id":"...","markdown":"Voy a verificar...","timestamp": 12345},
  --   {"type":"tool","id":"...","tool":{"name":"brave_search","status":"done","request":{...},"response":{...},"durationMs":420,"startedAt":12346},"timestamp":12346},
  --   ...
  -- ]
  segments            jsonb default '[]'::jsonb,

  -- Metadata
  duration_ms         int,
  total_tools_used    int default 0,
  total_tokens        int default 0,

  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);

-- Índices
create index if not exists idx_chat_messages_thread
  on chat_messages (thread_id, created_at);

create index if not exists idx_chat_messages_generation
  on chat_messages (generation_id) where generation_id is not null;

create index if not exists idx_chat_messages_firm_created
  on chat_messages (firm_id, created_at desc) where firm_id is not null;

create index if not exists idx_chat_messages_document
  on chat_messages (document_id) where document_id is not null;

-- Trigger updated_at
create or replace function update_chat_messages_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_chat_messages_updated_at on chat_messages;
create trigger trg_chat_messages_updated_at
  before update on chat_messages
  for each row execute function update_chat_messages_updated_at();

-- RLS: scope por firm_id (multi-tenant)
alter table chat_messages enable row level security;

do $$
begin
  -- Política: el usuario solo ve mensajes de su firm
  if not exists (
    select 1 from pg_policies
    where tablename = 'chat_messages' and policyname = 'chat_messages_firm_isolation'
  ) then
    create policy chat_messages_firm_isolation on chat_messages
      for all
      using (
        firm_id is null
        or firm_id::text = current_setting('request.jwt.claim.firm_id', true)
      );
  end if;
exception
  when others then
    raise notice 'chat_messages RLS policy skipped: %', sqlerrm;
end $$;

comment on table chat_messages is
  'Sprint M19.5: persistencia de threads del agente estilo Claude.
   Cada row es un mensaje compuesto (user prompt | assistant narrative).
   Los assistant messages tienen segments JSONB con paragraphs + tool calls.';

comment on column chat_messages.segments is
  'JSONB array de segments ordenados: [{type:"paragraph",markdown}|{type:"tool",tool:{...}}]';
