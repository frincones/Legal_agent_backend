-- ============================================================
-- LexAI · Sprint 16 · Colaboración del despacho
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Aporte:
--   1. comments               · hilos comentables sobre matter/document/canvas/lesson/kb_entry
--   2. mention_notifications  · cola por usuario de menciones no leídas
--   3. presence_sessions      · heartbeat con TTL 90s · "quién está viendo este caso"
--   4. activity_events        · tabla append-only emitida desde triggers + apis
--   5. RPCs lexai_activity_feed, lexai_active_users, lexai_unread_mentions
--   6. Triggers que pueblan activity_events cuando se crea/resuelve un comment
--   7. RLS firm-scoped + índices
-- ============================================================

-- ------------------------------------------------------------
-- 1. comments
-- ------------------------------------------------------------
create table if not exists comments (
  id                  uuid primary key default gen_random_uuid(),
  firm_id             uuid not null references firms(id) on delete cascade,
  -- anchor_kind decide a qué objeto está anclado el hilo
  anchor_kind         text not null
    check (anchor_kind in ('matter','matter_document','canvas','lesson','kb_entry')),
  matter_id           uuid references matters(id) on delete cascade,
  matter_document_id  uuid references matter_documents(id) on delete cascade,
  lesson_id           uuid,  -- case_lessons.id  (FK opcional · case_lessons en sprint 15)
  kb_entry_id         uuid,  -- knowledge_entries.id (FK opcional)
  -- anchor_ref para pintar highlights · ej: { page: 3, range: [120, 168] }
  anchor_ref          jsonb default '{}'::jsonb,
  -- threading
  parent_id           uuid references comments(id) on delete cascade,
  thread_root_id      uuid,  -- self si parent_id is null, sino el id de la raíz · para listar
  -- contenido
  body                text not null,
  mentions            uuid[] default array[]::uuid[],  -- user_ids extraídos del body
  -- resolución
  resolved            boolean not null default false,
  resolved_by         uuid references users(id) on delete set null,
  resolved_at         timestamptz,
  -- meta
  edited_at           timestamptz,
  created_by          uuid references users(id) on delete set null,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
-- Índices de lectura
create index if not exists comments_firm_created_idx
  on comments (firm_id, created_at desc);
create index if not exists comments_matter_idx
  on comments (matter_id, resolved, created_at desc) where matter_id is not null;
create index if not exists comments_doc_idx
  on comments (matter_document_id, created_at desc) where matter_document_id is not null;
create index if not exists comments_thread_idx
  on comments (thread_root_id, created_at);
create index if not exists comments_mentions_gin
  on comments using gin (mentions);

-- ------------------------------------------------------------
-- 2. mention_notifications · cola por usuario mencionado
-- ------------------------------------------------------------
create table if not exists mention_notifications (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid not null references users(id) on delete cascade,
  comment_id      uuid not null references comments(id) on delete cascade,
  matter_id       uuid references matters(id) on delete cascade,
  matter_document_id uuid references matter_documents(id) on delete cascade,
  body_preview    text,                    -- snippet del comment al momento de mencionar
  mentioned_by    uuid references users(id) on delete set null,
  read_at         timestamptz,
  created_at      timestamptz default now(),
  unique (user_id, comment_id)
);
create index if not exists mention_inbox_idx
  on mention_notifications (user_id, read_at, created_at desc);

-- ------------------------------------------------------------
-- 3. presence_sessions · heartbeat ligero
-- ------------------------------------------------------------
-- Una fila por usuario+matter (last_heartbeat se actualiza). TTL filtramos por <90s en query.
create table if not exists presence_sessions (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid not null references users(id) on delete cascade,
  matter_id       uuid references matters(id) on delete cascade,
  location_kind   text not null default 'matter'
    check (location_kind in ('matter','matter_document','canvas','dashboard','other')),
  location_ref    text,                                       -- ej: matter_document_id
  started_at      timestamptz default now(),
  last_heartbeat  timestamptz default now(),
  unique (user_id, matter_id, location_kind, location_ref)
);
create index if not exists presence_active_idx
  on presence_sessions (firm_id, matter_id, last_heartbeat desc);

-- ------------------------------------------------------------
-- 4. activity_events · feed unificado del despacho
-- ------------------------------------------------------------
create table if not exists activity_events (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  ts              timestamptz default now(),
  actor_user_id   uuid references users(id) on delete set null,
  kind            text not null
    check (kind in (
      'comment_added','comment_resolved','comment_edited',
      'doc_uploaded','doc_analyzed',
      'matter_status_changed','matter_created','matter_assigned',
      'lesson_extracted','lesson_added',
      'kb_entry_added','kb_entry_pinned',
      'event_added','deadline_added','deadline_completed',
      'invoice_sent','invoice_paid',
      'signature_sent','signature_signed',
      'other'
    )),
  matter_id       uuid references matters(id) on delete cascade,
  matter_document_id uuid references matter_documents(id) on delete cascade,
  target_kind     text,                                       -- comment, lesson, etc.
  target_id       uuid,                                       -- id del objeto que originó el evento
  title           text,                                       -- "Pedro comentó en …"
  preview         text,                                       -- snippet
  payload         jsonb default '{}'::jsonb
);
create index if not exists activity_firm_ts_idx
  on activity_events (firm_id, ts desc);
create index if not exists activity_matter_idx
  on activity_events (matter_id, ts desc) where matter_id is not null;
create index if not exists activity_actor_idx
  on activity_events (actor_user_id, ts desc) where actor_user_id is not null;

-- ============================================================
-- RLS · firm-scoped
-- ============================================================
alter table comments              enable row level security;
alter table mention_notifications enable row level security;
alter table presence_sessions     enable row level security;
alter table activity_events       enable row level security;

drop policy if exists com_select on comments;
drop policy if exists com_modify on comments;
create policy com_select on comments for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy com_modify on comments for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists mn_select on mention_notifications;
drop policy if exists mn_modify on mention_notifications;
create policy mn_select on mention_notifications for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy mn_modify on mention_notifications for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ps_select on presence_sessions;
drop policy if exists ps_modify on presence_sessions;
create policy ps_select on presence_sessions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ps_modify on presence_sessions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ae_select on activity_events;
drop policy if exists ae_modify on activity_events;
create policy ae_select on activity_events for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ae_modify on activity_events for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
-- firm_id auto-fill
drop trigger if exists trg_com_firm_id on comments;
create trigger trg_com_firm_id before insert on comments
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_com_updated_at on comments;
create trigger trg_com_updated_at before update on comments
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_mn_firm_id on mention_notifications;
create trigger trg_mn_firm_id before insert on mention_notifications
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ps_firm_id on presence_sessions;
create trigger trg_ps_firm_id before insert on presence_sessions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ae_firm_id on activity_events;
create trigger trg_ae_firm_id before insert on activity_events
  for each row execute function set_firm_id_from_jwt();

-- thread_root_id auto-fill (self si raíz, sino hereda)
create or replace function tg_comments_set_thread_root() returns trigger
language plpgsql as $$
declare
  v_root uuid;
begin
  if new.parent_id is null then
    new.thread_root_id := new.id;
  else
    select coalesce(thread_root_id, id) into v_root
      from comments where id = new.parent_id;
    new.thread_root_id := coalesce(v_root, new.id);
  end if;
  return new;
end;
$$;
drop trigger if exists trg_com_thread_root on comments;
create trigger trg_com_thread_root before insert on comments
  for each row execute function tg_comments_set_thread_root();

-- Cuando se inserta un comment con mentions: poblar mention_notifications + activity_events
create or replace function tg_comment_after_insert() returns trigger
language plpgsql as $$
declare
  v_uid uuid;
  v_preview text;
begin
  v_preview := left(coalesce(new.body,''), 280);
  -- Activity event
  insert into activity_events
    (firm_id, actor_user_id, kind, matter_id, matter_document_id,
     target_kind, target_id, title, preview, payload)
  values
    (new.firm_id, new.created_by, 'comment_added',
     new.matter_id, new.matter_document_id,
     'comment', new.id,
     case
       when new.anchor_kind = 'matter_document' then 'Comentó en documento'
       when new.anchor_kind = 'canvas' then 'Comentó en canvas'
       when new.anchor_kind = 'lesson' then 'Comentó en lección'
       when new.anchor_kind = 'kb_entry' then 'Comentó en entrada KB'
       else 'Comentó en caso'
     end,
     v_preview,
     jsonb_build_object(
       'comment_id', new.id,
       'anchor_kind', new.anchor_kind,
       'thread_root_id', new.thread_root_id,
       'parent_id', new.parent_id,
       'mentions_count', coalesce(array_length(new.mentions,1), 0)
     ));
  -- Mention notifications
  if new.mentions is not null and array_length(new.mentions, 1) > 0 then
    foreach v_uid in array new.mentions loop
      if v_uid <> coalesce(new.created_by, '00000000-0000-0000-0000-000000000000'::uuid) then
        insert into mention_notifications
          (firm_id, user_id, comment_id, matter_id, matter_document_id,
           body_preview, mentioned_by)
        values
          (new.firm_id, v_uid, new.id, new.matter_id, new.matter_document_id,
           v_preview, new.created_by)
        on conflict (user_id, comment_id) do nothing;
      end if;
    end loop;
  end if;
  return new;
end;
$$;
drop trigger if exists trg_com_after_insert on comments;
create trigger trg_com_after_insert after insert on comments
  for each row execute function tg_comment_after_insert();

-- Cuando se resuelve/edita un comment: activity event
create or replace function tg_comment_after_update() returns trigger
language plpgsql as $$
begin
  if old.resolved = false and new.resolved = true then
    insert into activity_events
      (firm_id, actor_user_id, kind, matter_id, matter_document_id,
       target_kind, target_id, title, preview)
    values
      (new.firm_id, new.resolved_by, 'comment_resolved',
       new.matter_id, new.matter_document_id,
       'comment', new.id, 'Resolvió un comentario',
       left(coalesce(new.body,''), 200));
  elsif old.body is distinct from new.body then
    insert into activity_events
      (firm_id, actor_user_id, kind, matter_id, matter_document_id,
       target_kind, target_id, title, preview)
    values
      (new.firm_id, new.created_by, 'comment_edited',
       new.matter_id, new.matter_document_id,
       'comment', new.id, 'Editó un comentario',
       left(coalesce(new.body,''), 200));
  end if;
  return new;
end;
$$;
drop trigger if exists trg_com_after_update on comments;
create trigger trg_com_after_update after update on comments
  for each row execute function tg_comment_after_update();

-- ============================================================
-- RPCs
-- ============================================================

-- Feed unificado de actividad del despacho · paginación cursor-style por ts
create or replace function lexai_activity_feed(
  p_firm_id     uuid,
  p_matter_id   uuid default null,
  p_actor_id    uuid default null,
  p_kinds       text[] default null,
  p_since       timestamptz default null,
  p_limit       int default 50
) returns table (
  id uuid,
  ts timestamptz,
  actor_user_id uuid,
  kind text,
  matter_id uuid,
  matter_document_id uuid,
  target_kind text,
  target_id uuid,
  title text,
  preview text,
  payload jsonb,
  actor_name text,
  actor_avatar text
)
language sql stable as $$
  select a.id, a.ts, a.actor_user_id, a.kind, a.matter_id, a.matter_document_id,
         a.target_kind, a.target_id, a.title, a.preview, a.payload,
         u.full_name as actor_name, u.avatar_url as actor_avatar
    from activity_events a
    left join users u on u.id = a.actor_user_id
   where a.firm_id = p_firm_id
     and (p_matter_id is null or a.matter_id = p_matter_id)
     and (p_actor_id is null or a.actor_user_id = p_actor_id)
     and (p_kinds is null or a.kind = any(p_kinds))
     and (p_since is null or a.ts > p_since)
   order by a.ts desc
   limit p_limit;
$$;

-- Usuarios activos en un matter en los últimos 90 segundos
create or replace function lexai_active_users(
  p_firm_id   uuid,
  p_matter_id uuid,
  p_window_seconds int default 90
) returns table (
  user_id uuid,
  full_name text,
  avatar_url text,
  location_kind text,
  location_ref text,
  last_heartbeat timestamptz
)
language sql stable as $$
  select distinct on (p.user_id)
         p.user_id, u.full_name, u.avatar_url,
         p.location_kind, p.location_ref, p.last_heartbeat
    from presence_sessions p
    join users u on u.id = p.user_id
   where p.firm_id = p_firm_id
     and p.matter_id = p_matter_id
     and p.last_heartbeat > now() - make_interval(secs => p_window_seconds)
   order by p.user_id, p.last_heartbeat desc;
$$;

-- Contador de menciones no leídas del usuario actual
create or replace function lexai_unread_mentions(
  p_firm_id uuid,
  p_user_id uuid
) returns int
language sql stable as $$
  select count(*)::int
    from mention_notifications
   where firm_id = p_firm_id
     and user_id = p_user_id
     and read_at is null;
$$;

-- Counts por matter para badges en la lista de casos
create or replace function lexai_matter_collab_counts(
  p_firm_id   uuid,
  p_matter_id uuid
) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'comments_total', (select count(*) from comments
                         where firm_id = p_firm_id and matter_id = p_matter_id),
    'comments_open',  (select count(*) from comments
                         where firm_id = p_firm_id and matter_id = p_matter_id
                           and resolved = false),
    'active_users',   (select count(*) from lexai_active_users(p_firm_id, p_matter_id, 90))
  );
$$;
