-- ============================================================
-- LexAI · Sprint 7 · Calendar Sync + WhatsApp + Analytics + Global Search
-- Migration date: 2026-05-10
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. calendar_integrations · cuentas Google Calendar / Outlook
-- ------------------------------------------------------------
create table if not exists calendar_integrations (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  provider          text not null check (provider in ('google','outlook')),
  email_address     text not null,
  display_name      text,
  oauth_access_token   text,
  oauth_refresh_token  text,
  oauth_access_token_enc  bytea,
  oauth_refresh_token_enc bytea,
  oauth_expires_at  timestamptz,
  encryption_version int default 0,
  primary_calendar_id text,
  watched_calendar_ids text[] default array[]::text[],
  scopes            text[] default array[]::text[],
  active            boolean not null default true,
  status            text not null default 'pending'
    check (status in ('pending','connected','expired','revoked','error')),
  last_status       text,
  last_error        text,
  last_synced_at    timestamptz,
  sync_token        text,                                  -- delta sync cursor (Google)
  delta_link        text,                                  -- delta sync cursor (Outlook)
  auto_create_deadlines boolean not null default true,     -- eventos con #lexai → matter_deadlines
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (user_id, provider, email_address)
);
create index if not exists cal_integ_firm_active_idx
  on calendar_integrations (firm_id, active) where active = true;

-- ------------------------------------------------------------
-- 2. calendar_events · cache de eventos sincronizados
-- ------------------------------------------------------------
create table if not exists calendar_events (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  integration_id    uuid not null references calendar_integrations(id) on delete cascade,
  matter_id         uuid references matters(id) on delete set null,
  external_id       text not null,
  calendar_id       text,
  title             text,
  description       text,
  location          text,
  start_at          timestamptz not null,
  end_at            timestamptz,
  is_all_day        boolean default false,
  attendees         jsonb default '[]'::jsonb,
  meeting_url       text,
  status            text default 'confirmed',              -- 'confirmed','tentative','canceled'
  source_tag        text,                                  -- '#lexai' detectado en title/desc
  deadline_id       uuid references matter_deadlines(id) on delete set null,
  raw               jsonb default '{}'::jsonb,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (integration_id, external_id)
);
create index if not exists cal_events_firm_time_idx
  on calendar_events (firm_id, start_at desc);
create index if not exists cal_events_matter_idx
  on calendar_events (matter_id) where matter_id is not null;
create index if not exists cal_events_upcoming_idx
  on calendar_events (firm_id, start_at);

-- ------------------------------------------------------------
-- 3. whatsapp_integrations · WhatsApp Business Cloud (Meta)
-- ------------------------------------------------------------
create table if not exists whatsapp_integrations (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null unique references firms(id) on delete cascade,
  phone_number_id   text not null,                         -- de Meta
  display_phone     text,                                  -- +57...
  waba_id           text,                                  -- WhatsApp Business Account id
  access_token      text,
  access_token_enc  bytea,
  encryption_version int default 0,
  webhook_verify_token text,                               -- shared secret para verify hook
  active            boolean not null default true,
  status            text not null default 'pending'
    check (status in ('pending','connected','expired','revoked','error')),
  last_status       text,
  last_error        text,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);

-- ------------------------------------------------------------
-- 4. whatsapp_messages · mensajes inbound/outbound
-- ------------------------------------------------------------
create table if not exists whatsapp_messages (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  integration_id    uuid not null references whatsapp_integrations(id) on delete cascade,
  client_id         uuid references clients(id) on delete set null,
  matter_id         uuid references matters(id) on delete set null,
  wa_message_id     text not null,
  direction         text not null check (direction in ('inbound','outbound')),
  from_phone        text,
  to_phone          text,
  body              text,
  media_url         text,
  template_name     text,                                  -- si es plantilla aprobada por Meta
  status            text default 'sent'
    check (status in ('queued','sent','delivered','read','failed')),
  error             text,
  raw               jsonb default '{}'::jsonb,
  occurred_at       timestamptz not null default now(),
  unique (integration_id, wa_message_id)
);
create index if not exists wa_msg_firm_time_idx
  on whatsapp_messages (firm_id, occurred_at desc);
create index if not exists wa_msg_client_idx
  on whatsapp_messages (client_id, occurred_at desc) where client_id is not null;
create index if not exists wa_msg_matter_idx
  on whatsapp_messages (matter_id, occurred_at desc) where matter_id is not null;
create index if not exists wa_msg_inbound_unread_idx
  on whatsapp_messages (firm_id, occurred_at desc) where direction = 'inbound';

-- ------------------------------------------------------------
-- 5. firm_reports · agregados precalculados (refresh nightly)
-- ------------------------------------------------------------
create table if not exists firm_reports (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  kind              text not null,                         -- 'overview' | 'lawyer_perf' | 'deadlines' | 'citations' | 'billing'
  period_start      date not null,
  period_end        date not null,
  data              jsonb not null,
  generated_at      timestamptz default now(),
  unique (firm_id, kind, period_start, period_end)
);
create index if not exists firm_reports_lookup_idx
  on firm_reports (firm_id, kind, period_end desc);

-- ============================================================
-- RLS
-- ============================================================
alter table calendar_integrations enable row level security;
alter table calendar_events       enable row level security;
alter table whatsapp_integrations enable row level security;
alter table whatsapp_messages     enable row level security;
alter table firm_reports          enable row level security;

drop policy if exists cal_integ_select on calendar_integrations;
drop policy if exists cal_integ_modify on calendar_integrations;
create policy cal_integ_select on calendar_integrations for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cal_integ_modify on calendar_integrations for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists cal_events_select on calendar_events;
drop policy if exists cal_events_modify on calendar_events;
create policy cal_events_select on calendar_events for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy cal_events_modify on calendar_events for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists wa_integ_select on whatsapp_integrations;
drop policy if exists wa_integ_modify on whatsapp_integrations;
create policy wa_integ_select on whatsapp_integrations for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy wa_integ_modify on whatsapp_integrations for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists wa_msg_select on whatsapp_messages;
drop policy if exists wa_msg_modify on whatsapp_messages;
create policy wa_msg_select on whatsapp_messages for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy wa_msg_modify on whatsapp_messages for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists firm_reports_select on firm_reports;
drop policy if exists firm_reports_modify on firm_reports;
create policy firm_reports_select on firm_reports for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy firm_reports_modify on firm_reports for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_cal_integ_firm_id on calendar_integrations;
create trigger trg_cal_integ_firm_id
  before insert on calendar_integrations
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_cal_integ_updated_at on calendar_integrations;
create trigger trg_cal_integ_updated_at
  before update on calendar_integrations
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_cal_events_firm_id on calendar_events;
create trigger trg_cal_events_firm_id
  before insert on calendar_events
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_cal_events_updated_at on calendar_events;
create trigger trg_cal_events_updated_at
  before update on calendar_events
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_wa_integ_firm_id on whatsapp_integrations;
create trigger trg_wa_integ_firm_id
  before insert on whatsapp_integrations
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_wa_integ_updated_at on whatsapp_integrations;
create trigger trg_wa_integ_updated_at
  before update on whatsapp_integrations
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_wa_msg_firm_id on whatsapp_messages;
create trigger trg_wa_msg_firm_id
  before insert on whatsapp_messages
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_firm_reports_firm_id on firm_reports;
create trigger trg_firm_reports_firm_id
  before insert on firm_reports
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- Global search · función híbrida (FTS + trgm)
-- ============================================================

-- Extension pg_trgm ya existe del Sprint 2.

create or replace function lexai_global_search(
  p_firm_id uuid,
  p_query   text,
  p_limit   int default 30
) returns table (
  kind        text,
  id          uuid,
  title       text,
  snippet     text,
  matter_id   uuid,
  client_id   uuid,
  rank        real
)
language sql stable as $$
  with q as (select coalesce(nullif(trim(p_query),''),'') as text),
       qts as (select plainto_tsquery('spanish', (select text from q)) as ts),
       matters_hit as (
         select 'matter'::text as kind, m.id, m.titulo as title,
                coalesce(m.expediente,'') as snippet,
                m.id as matter_id, m.client_id,
                ts_rank(to_tsvector('spanish', coalesce(m.titulo,'') || ' ' || coalesce(m.expediente,'')), (select ts from qts))::real as rank
           from matters m
          where m.firm_id = p_firm_id
            and (
              (select ts from qts) @@ to_tsvector('spanish', coalesce(m.titulo,'') || ' ' || coalesce(m.expediente,''))
              or m.titulo ilike '%' || (select text from q) || '%'
              or m.expediente ilike '%' || (select text from q) || '%'
            )
       ),
       clients_hit as (
         select 'client'::text as kind, c.id, c.nombre as title,
                coalesce(c.tax_id, c.personal_id, '') as snippet,
                null::uuid as matter_id, c.id as client_id,
                ts_rank(to_tsvector('spanish', coalesce(c.nombre,'') || ' ' || coalesce(c.tax_id,'') || ' ' || coalesce(c.personal_id,'')), (select ts from qts))::real as rank
           from clients c
          where c.firm_id = p_firm_id
            and (
              (select ts from qts) @@ to_tsvector('spanish', coalesce(c.nombre,'') || ' ' || coalesce(c.tax_id,'') || ' ' || coalesce(c.personal_id,''))
              or c.nombre ilike '%' || (select text from q) || '%'
              or c.tax_id ilike '%' || (select text from q) || '%'
              or c.personal_id ilike '%' || (select text from q) || '%'
            )
       ),
       docs_hit as (
         select 'document'::text as kind, d.id, d.titulo as title,
                substring(coalesce(d.resumen_ia,''), 1, 200) as snippet,
                d.matter_id, null::uuid as client_id,
                ts_rank(to_tsvector('spanish', coalesce(d.titulo,'') || ' ' || coalesce(d.resumen_ia,'')), (select ts from qts))::real as rank
           from matter_documents d
          where d.firm_id = p_firm_id
            and (
              (select ts from qts) @@ to_tsvector('spanish', coalesce(d.titulo,'') || ' ' || coalesce(d.resumen_ia,''))
              or d.titulo ilike '%' || (select text from q) || '%'
            )
       )
  select * from matters_hit
  union all
  select * from clients_hit
  union all
  select * from docs_hit
  order by rank desc nulls last
  limit p_limit;
$$;

-- ============================================================
-- Analytics · RPC para KPI overview
-- ============================================================

create or replace function lexai_firm_kpis(
  p_firm_id   uuid default null,
  p_days      int  default 30
) returns jsonb
language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
       since as (select (now() - (p_days::text || ' days')::interval) as ts)
  select jsonb_build_object(
    'matters_total', (select count(*) from matters where firm_id = (select id from f)),
    'matters_active', (select count(*) from matters where firm_id = (select id from f) and status = 'activo'),
    'matters_new',   (select count(*) from matters where firm_id = (select id from f) and created_at >= (select ts from since)),
    'clients_total', (select count(*) from clients where firm_id = (select id from f)),
    'clients_new',   (select count(*) from clients where firm_id = (select id from f) and created_at >= (select ts from since)),
    'deadlines_upcoming', (select count(*) from matter_deadlines
                            where firm_id = (select id from f) and completado = false
                              and fecha between current_date and current_date + 14),
    'deadlines_overdue',  (select count(*) from matter_deadlines
                             where firm_id = (select id from f) and completado = false
                               and fecha < current_date),
    'deadlines_done',     (select count(*) from matter_deadlines
                             where firm_id = (select id from f) and completado = true
                               and fecha >= (select ts from since)::date),
    'docs_uploaded',      (select count(*) from matter_documents
                             where firm_id = (select id from f) and created_at >= (select ts from since)),
    'by_materia',         coalesce((select jsonb_object_agg(materia, n) from (
                              select materia::text, count(*) as n from matters
                               where firm_id = (select id from f) group by materia
                            ) x), '{}'::jsonb),
    'by_status',          coalesce((select jsonb_object_agg(status, n) from (
                              select status::text, count(*) as n from matters
                               where firm_id = (select id from f) group by status
                            ) x), '{}'::jsonb),
    'by_owner',           coalesce((select jsonb_object_agg(owner_user_id::text, n) from (
                              select owner_user_id, count(*) as n from matters
                               where firm_id = (select id from f) and owner_user_id is not null
                               group by owner_user_id
                            ) x), '{}'::jsonb),
    'period_days', p_days,
    'generated_at', now()
  );
$$;
