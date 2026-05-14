-- ============================================================
-- LexAI · Sprint 12 · PWA + Offline Queue + Push real
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. Extender push_subscriptions con metadata adicional (additive)
-- ------------------------------------------------------------
alter table push_subscriptions
  add column if not exists device_kind text default 'web' check (device_kind in ('web','android','ios','desktop_pwa')),
  add column if not exists app_version text,
  add column if not exists platform text,
  add column if not exists install_source text;

-- ------------------------------------------------------------
-- 2. offline_sync_jobs · cola de operaciones diferidas (queued offline)
-- ------------------------------------------------------------
create table if not exists offline_sync_jobs (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  client_request_id text not null,                          -- idempotency key (cliente genera UUID v4)
  method            text not null check (method in ('POST','PATCH','PUT','DELETE')),
  url               text not null,                          -- '/v1/leads', '/v1/time-entries', etc.
  payload           jsonb,
  status            text not null default 'queued'
    check (status in ('queued','processing','succeeded','failed','skipped')),
  status_code       int,
  response          jsonb,
  error             text,
  attempts          int not null default 0,
  max_attempts      int not null default 3,
  enqueued_at       timestamptz not null default now(),
  started_at        timestamptz,
  completed_at      timestamptz,
  unique (user_id, client_request_id)
);
create index if not exists sync_jobs_queue_idx
  on offline_sync_jobs (user_id, status, enqueued_at)
  where status in ('queued','processing');
create index if not exists sync_jobs_firm_idx
  on offline_sync_jobs (firm_id, enqueued_at desc);

-- ------------------------------------------------------------
-- 3. push_notifications_log · auditoría de pushes enviadas
-- ------------------------------------------------------------
create table if not exists push_notifications_log (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  subscription_id   uuid references push_subscriptions(id) on delete set null,
  title             text not null,
  body              text,
  url               text,
  kind              text,                                    -- 'sla_alert', 'judicial', 'inbox', 'test', 'insight'
  status            text not null default 'sent'
    check (status in ('sent','failed','expired')),
  http_status       int,
  error             text,
  sent_at           timestamptz default now()
);
create index if not exists push_log_firm_time_idx
  on push_notifications_log (firm_id, sent_at desc);
create index if not exists push_log_user_idx
  on push_notifications_log (user_id, sent_at desc);

-- ============================================================
-- RLS
-- ============================================================
alter table offline_sync_jobs       enable row level security;
alter table push_notifications_log  enable row level security;

drop policy if exists osj_select on offline_sync_jobs;
drop policy if exists osj_modify on offline_sync_jobs;
create policy osj_select on offline_sync_jobs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy osj_modify on offline_sync_jobs for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists pn_log_select on push_notifications_log;
create policy pn_log_select on push_notifications_log for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill
-- ============================================================
drop trigger if exists trg_osj_firm_id on offline_sync_jobs;
create trigger trg_osj_firm_id before insert on offline_sync_jobs
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_pn_log_firm_id on push_notifications_log;
create trigger trg_pn_log_firm_id before insert on push_notifications_log
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPC · estado de cola del usuario actual
-- ============================================================
create or replace function lexai_sync_queue_stats(p_user_id uuid default null)
returns jsonb language sql stable as $$
  with u as (select coalesce(p_user_id, auth.uid()) as id)
  select jsonb_build_object(
    'queued',     (select count(*) from offline_sync_jobs where user_id = (select id from u) and status = 'queued'),
    'processing', (select count(*) from offline_sync_jobs where user_id = (select id from u) and status = 'processing'),
    'failed',     (select count(*) from offline_sync_jobs where user_id = (select id from u) and status = 'failed'),
    'succeeded_24h', (select count(*) from offline_sync_jobs
                       where user_id = (select id from u) and status = 'succeeded'
                         and completed_at >= now() - interval '24 hours')
  );
$$;
