-- ============================================================
-- LexAI · Sprint 5 · Court Watcher + Email + Push + Inbox
-- Migration date: 2026-05-10
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. email_integrations · cuentas conectadas por usuario (Gmail/Outlook/IMAP)
-- ------------------------------------------------------------
create table if not exists email_integrations (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  provider          text not null check (provider in ('gmail','outlook','imap')),
  email_address     text not null,
  display_name      text,
  oauth_access_token   text,                 -- cifrado en app layer (Sprint 6)
  oauth_refresh_token  text,
  oauth_expires_at  timestamptz,
  imap_host         text,
  imap_port         int,
  imap_username     text,
  imap_password_enc text,                    -- cifrado en app layer
  scopes            text[] default array[]::text[],
  active            boolean not null default true,
  status            text not null default 'pending'
    check (status in ('pending','connected','expired','revoked','error')),
  last_status       text,
  last_error        text,
  last_synced_at    timestamptz,
  watch_label       text default 'INBOX',
  filter_query      text,                    -- 'from:@ramajudicial.gov.co OR subject:notificación'
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (user_id, provider, email_address)
);
create index if not exists email_integrations_firm_idx
  on email_integrations (firm_id, active) where active = true;
create index if not exists email_integrations_user_idx
  on email_integrations (user_id, active) where active = true;

-- ------------------------------------------------------------
-- 2. email_messages · mensajes ingresados (parser legal)
-- ------------------------------------------------------------
create table if not exists email_messages (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  integration_id    uuid not null references email_integrations(id) on delete cascade,
  matter_id         uuid references matters(id) on delete set null,
  external_id       text not null,                 -- id de Gmail/Outlook
  thread_id         text,
  from_address      text,
  to_addresses      text[] default array[]::text[],
  cc_addresses      text[] default array[]::text[],
  subject           text,
  snippet           text,
  body_text         text,
  body_html         text,
  received_at       timestamptz,
  has_attachments   boolean default false,
  attachments_meta  jsonb default '[]'::jsonb,     -- [{name,size,mime,storage_path}]
  is_legal          boolean default false,
  legal_kind        text,                          -- 'auto','sentencia','citacion','requerimiento','traslado','otro'
  matched_expediente text,
  matched_juzgado   text,
  matched_fecha     date,
  parsed_summary    text,
  severidad         text default 'info'
    check (severidad in ('info','alta','critica')),
  status            text not null default 'unread'
    check (status in ('unread','read','archived','snoozed')),
  parser_version    text default 'v1',
  parser_metadata   jsonb default '{}'::jsonb,
  created_at        timestamptz default now(),
  read_at           timestamptz,
  unique (integration_id, external_id)
);
create index if not exists email_messages_firm_idx
  on email_messages (firm_id, received_at desc);
create index if not exists email_messages_matter_idx
  on email_messages (matter_id) where matter_id is not null;
create index if not exists email_messages_unread_idx
  on email_messages (firm_id, status) where status = 'unread';
create index if not exists email_messages_legal_idx
  on email_messages (firm_id, is_legal, received_at desc) where is_legal = true;

-- ------------------------------------------------------------
-- 3. judicial_snapshots · capturas raw del scraper para diff
-- ------------------------------------------------------------
create table if not exists judicial_snapshots (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  subscription_id   uuid not null references judicial_subscriptions(id) on delete cascade,
  fetched_at        timestamptz default now(),
  status_code       int,
  raw_hash          text not null,                 -- sha256 del cuerpo normalizado
  raw_body          text,                          -- HTML/JSON crudo (truncado 64KB)
  parsed            jsonb default '{}'::jsonb,     -- {expediente, ultima_actuacion, fecha, partes}
  diff_changed      boolean default false,
  notif_count       int default 0,
  created_at        timestamptz default now()
);
create index if not exists judicial_snap_sub_idx
  on judicial_snapshots (subscription_id, fetched_at desc);
create index if not exists judicial_snap_firm_idx
  on judicial_snapshots (firm_id, fetched_at desc);

-- ------------------------------------------------------------
-- 4. push_subscriptions · suscripciones Web Push (VAPID)
-- ------------------------------------------------------------
create table if not exists push_subscriptions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  endpoint          text not null,
  p256dh            text not null,
  auth              text not null,
  user_agent        text,
  device_label      text,
  active            boolean not null default true,
  last_pinged_at    timestamptz,
  last_status       int,
  last_error        text,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (user_id, endpoint)
);
create index if not exists push_subs_user_idx
  on push_subscriptions (user_id, active) where active = true;
create index if not exists push_subs_firm_idx
  on push_subscriptions (firm_id, active) where active = true;

-- ============================================================
-- RLS
-- ============================================================
alter table email_integrations  enable row level security;
alter table email_messages       enable row level security;
alter table judicial_snapshots   enable row level security;
alter table push_subscriptions   enable row level security;

drop policy if exists email_integrations_select on email_integrations;
drop policy if exists email_integrations_modify on email_integrations;
create policy email_integrations_select on email_integrations for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy email_integrations_modify on email_integrations for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists email_messages_select on email_messages;
drop policy if exists email_messages_modify on email_messages;
create policy email_messages_select on email_messages for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy email_messages_modify on email_messages for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists judicial_snap_select on judicial_snapshots;
drop policy if exists judicial_snap_modify on judicial_snapshots;
create policy judicial_snap_select on judicial_snapshots for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy judicial_snap_modify on judicial_snapshots for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists push_subs_select on push_subscriptions;
drop policy if exists push_subs_modify on push_subscriptions;
create policy push_subs_select on push_subscriptions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy push_subs_modify on push_subscriptions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_email_integrations_firm_id on email_integrations;
create trigger trg_email_integrations_firm_id
  before insert on email_integrations
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_email_integrations_updated_at on email_integrations;
create trigger trg_email_integrations_updated_at
  before update on email_integrations
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_email_messages_firm_id on email_messages;
create trigger trg_email_messages_firm_id
  before insert on email_messages
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_judicial_snap_firm_id on judicial_snapshots;
create trigger trg_judicial_snap_firm_id
  before insert on judicial_snapshots
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_push_subs_firm_id on push_subscriptions;
create trigger trg_push_subs_firm_id
  before insert on push_subscriptions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_push_subs_updated_at on push_subscriptions;
create trigger trg_push_subs_updated_at
  before update on push_subscriptions
  for each row execute function tg_set_updated_at();
