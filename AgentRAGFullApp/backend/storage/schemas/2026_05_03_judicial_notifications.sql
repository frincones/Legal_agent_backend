-- ============================================================
-- LexAI · F2 · Judicial notifications poller
-- Migration date: 2026-05-03
-- Idempotent · additive · NO DROP
-- ============================================================

-- 1. judicial_subscriptions · qué expedientes monitoreamos por firm
create table if not exists judicial_subscriptions (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  matter_id       uuid references matters(id) on delete cascade,
  fuente          text not null,                  -- 'rama_judicial' | 'tyba' | 'samai' | 'dof_co' | 'sic'
  expediente      text,                            -- '11001-31-05-012-2026-00473-00'
  juzgado         text,                            -- 'Juzgado 12 Laboral del Circuito de Bogotá'
  ciudad          text,                            -- 'Bogotá'
  query_extra     jsonb default '{}'::jsonb,
  active          boolean not null default true,
  last_polled_at  timestamptz,
  last_status     text,                            -- 'ok' | 'error' | 'rate_limited'
  last_error      text,
  poll_count      int not null default 0,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),
  unique (firm_id, fuente, expediente)
);
create index if not exists judicial_subs_active_idx
  on judicial_subscriptions (active, last_polled_at) where active = true;
create index if not exists judicial_subs_matter_idx
  on judicial_subscriptions (matter_id) where matter_id is not null;

-- 2. judicial_notifications · cada hit detectado
create table if not exists judicial_notifications (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  matter_id         uuid references matters(id) on delete cascade,
  subscription_id   uuid references judicial_subscriptions(id) on delete set null,
  fuente            text not null,
  titulo            text not null,
  resumen           text,
  url_oficial       text,
  fecha_publicacion date,
  fecha_actuacion   date,
  expediente        text,
  juzgado           text,
  tipo              text,                          -- 'auto', 'sentencia', 'notificacion', 'edicto', 'oficio', 'otro'
  severidad         text not null default 'info'   -- 'info' | 'alta' | 'critica'
    check (severidad in ('info','alta','critica')),
  status            text not null default 'unread'
    check (status in ('unread','read','archived','snoozed')),
  raw_html          text,
  hash_dedup        text not null,                  -- sha256(url|titulo|fecha) para evitar duplicados
  metadata          jsonb default '{}'::jsonb,
  created_at        timestamptz default now(),
  read_at           timestamptz,
  unique (firm_id, hash_dedup)
);
create index if not exists judicial_notif_firm_idx
  on judicial_notifications (firm_id, created_at desc);
create index if not exists judicial_notif_matter_idx
  on judicial_notifications (matter_id) where matter_id is not null;
create index if not exists judicial_notif_unread_idx
  on judicial_notifications (firm_id, status) where status = 'unread';
create index if not exists judicial_notif_severidad_idx
  on judicial_notifications (firm_id, severidad) where severidad in ('alta','critica');

-- ============================================================
-- RLS
-- ============================================================
alter table judicial_subscriptions  enable row level security;
alter table judicial_notifications  enable row level security;

drop policy if exists judicial_subs_select on judicial_subscriptions;
drop policy if exists judicial_subs_modify on judicial_subscriptions;
create policy judicial_subs_select on judicial_subscriptions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy judicial_subs_modify on judicial_subscriptions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists judicial_notif_select on judicial_notifications;
drop policy if exists judicial_notif_modify on judicial_notifications;
create policy judicial_notif_select on judicial_notifications for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy judicial_notif_modify on judicial_notifications for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_judicial_subs_firm_id on judicial_subscriptions;
create trigger trg_judicial_subs_firm_id
  before insert on judicial_subscriptions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_judicial_notif_firm_id on judicial_notifications;
create trigger trg_judicial_notif_firm_id
  before insert on judicial_notifications
  for each row execute function set_firm_id_from_jwt();

-- updated_at en subscriptions
drop trigger if exists trg_judicial_subs_updated_at on judicial_subscriptions;
create trigger trg_judicial_subs_updated_at
  before update on judicial_subscriptions
  for each row execute function tg_set_updated_at();

-- ============================================================
-- RPC · contar no leídas por firm + por matter
-- ============================================================
create or replace function lexai_judicial_counts(p_firm_id uuid default null)
returns jsonb
language sql
stable
as $$
  with f as (
    select coalesce(p_firm_id, auth_firm_id()) as id
  )
  select jsonb_build_object(
    'unread_total', (select count(*) from judicial_notifications
                       where firm_id = (select id from f)
                         and status = 'unread'),
    'critical_total', (select count(*) from judicial_notifications
                         where firm_id = (select id from f)
                           and status = 'unread'
                           and severidad = 'critica'),
    'unread_today', (select count(*) from judicial_notifications
                       where firm_id = (select id from f)
                         and status = 'unread'
                         and created_at >= current_date),
    'by_matter', (select coalesce(jsonb_object_agg(matter_id, n), '{}'::jsonb) from (
                    select matter_id::text, count(*) as n
                    from judicial_notifications
                    where firm_id = (select id from f)
                      and status = 'unread'
                      and matter_id is not null
                    group by matter_id
                  ) x)
  );
$$;
