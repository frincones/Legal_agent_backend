-- ============================================================
-- LexAI · Sprint M19.6 · Add UNIQUE constraint en chat_messages.thread_id
-- Migration date: 2026-05-27
-- Idempotent · additive
-- ============================================================
--
-- Necesario para soportar UPSERT en persist_chat_thread.
-- Cada thread genera 2 rows (user con thread_id="foo:user", assistant con
-- thread_id="foo"), así que SON únicos por construcción.

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'chat_messages_thread_id_key'
       or (conrelid = 'chat_messages'::regclass and contype = 'u' and conname like '%thread_id%')
  ) then
    -- Solo agregar si no hay duplicados (defensivo)
    if not exists (
      select thread_id, count(*) from chat_messages
      group by thread_id having count(*) > 1
      limit 1
    ) then
      alter table chat_messages add constraint chat_messages_thread_id_key unique (thread_id);
      raise notice 'chat_messages_thread_id_key UNIQUE added';
    else
      raise notice 'chat_messages.thread_id NO unique: hay duplicados, skip constraint';
    end if;
  else
    raise notice 'chat_messages.thread_id ya tiene UNIQUE constraint';
  end if;
exception
  when others then
    raise notice 'chat_messages UNIQUE constraint skipped: %', sqlerrm;
end $$;
