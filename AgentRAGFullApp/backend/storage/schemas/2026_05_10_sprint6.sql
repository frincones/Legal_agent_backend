-- ============================================================
-- LexAI · Sprint 6 · Billing (Paddle) + Habeas Data + Quotas + Email Encryption
-- Migration date: 2026-05-10
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. subscription_plans · catálogo de planes (seed estático)
-- ------------------------------------------------------------
create table if not exists subscription_plans (
  code            text primary key,                       -- 'free' | 'pro' | 'firm' | 'enterprise'
  name            text not null,
  monthly_cop     int not null default 0,
  annual_cop      int not null default 0,
  paddle_price_id text,                                   -- p_xxx de Paddle Catalog
  paddle_price_id_annual text,
  -- Quotas (null = ilimitado)
  q_users         int,
  q_matters       int,
  q_documents_mo  int,                                    -- subidas mensuales
  q_llm_calls_mo  int,
  q_voice_min_mo  int,
  q_email_accounts int,
  q_judicial_subs int,
  -- Features (boolean flags)
  f_court_watcher boolean not null default true,
  f_email_ingest  boolean not null default false,
  f_voice         boolean not null default true,
  f_canvas        boolean not null default true,
  f_calc          boolean not null default true,
  f_briefing      boolean not null default true,
  f_priority_support boolean not null default false,
  created_at      timestamptz default now()
);

-- Seed básico (idempotente con on conflict do nothing)
insert into subscription_plans
  (code, name, monthly_cop, annual_cop,
   q_users, q_matters, q_documents_mo, q_llm_calls_mo, q_voice_min_mo,
   q_email_accounts, q_judicial_subs,
   f_email_ingest, f_priority_support)
values
  ('free',       'Free Trial',  0,        0,         1, 3,    20,  200,  60, 0, 3,  false, false),
  ('pro',        'Pro',         149000,   1490000,   1, 30,   500, 5000, 600,1, 30, true,  false),
  ('firm',       'Firma',       349000,   3490000,   5, 200,  2000,20000,2400,5,200,true, true),
  ('enterprise', 'Enterprise',  990000,   9900000,   null, null, null, null, null, null, null, true, true)
on conflict (code) do nothing;

-- ------------------------------------------------------------
-- 2. firm_subscriptions · estado de suscripción por firma
-- ------------------------------------------------------------
create table if not exists firm_subscriptions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null unique references firms(id) on delete cascade,
  plan_code         text not null references subscription_plans(code),
  status            text not null default 'active'
    check (status in ('active','trialing','past_due','canceled','paused','grace')),
  paddle_customer_id     text,
  paddle_subscription_id text,
  paddle_price_id   text,
  billing_period    text default 'monthly' check (billing_period in ('monthly','annual')),
  current_period_start timestamptz,
  current_period_end   timestamptz,
  trial_ends_at     timestamptz,
  canceled_at       timestamptz,
  -- snapshot de cuotas en caso de plan custom
  overrides         jsonb default '{}'::jsonb,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists firm_subs_status_idx on firm_subscriptions (status);
create index if not exists firm_subs_paddle_idx on firm_subscriptions (paddle_subscription_id);

-- ------------------------------------------------------------
-- 3. usage_events · ledger granular de uso (para billing + quotas)
-- ------------------------------------------------------------
create table if not exists usage_events (
  id                bigserial primary key,
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  kind              text not null,                        -- 'llm_call' | 'voice_minute' | 'document_upload' | 'email_sync' | 'judicial_poll' | 'canvas_generate'
  count             int not null default 1,
  cost_units        numeric(12,4) default 0,              -- tokens / segundos / bytes (depende de kind)
  metadata          jsonb default '{}'::jsonb,
  occurred_at       timestamptz not null default now()
);
create index if not exists usage_events_firm_kind_idx
  on usage_events (firm_id, kind, occurred_at desc);
create index if not exists usage_events_firm_period_idx
  on usage_events (firm_id, occurred_at desc);

-- ------------------------------------------------------------
-- 4. audit_logs · compliance Habeas Data (Ley 1581/2012 CO)
-- ------------------------------------------------------------
create table if not exists audit_logs (
  id                bigserial primary key,
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  action            text not null,                        -- 'client.read' | 'client.export' | 'matter.read' | 'document.download' | 'auth.login' | 'role.change'
  resource_type     text,                                 -- 'client' | 'matter' | 'document' | 'user' | 'firm'
  resource_id       text,
  ip_address        inet,
  user_agent        text,
  outcome           text not null default 'success' check (outcome in ('success','denied','error')),
  reason            text,
  data_subject_id   text,                                 -- ID del titular del dato (NIT/CC) — para habeas data
  metadata          jsonb default '{}'::jsonb,
  occurred_at       timestamptz not null default now()
);
create index if not exists audit_firm_time_idx
  on audit_logs (firm_id, occurred_at desc);
create index if not exists audit_firm_user_idx
  on audit_logs (firm_id, user_id, occurred_at desc) where user_id is not null;
create index if not exists audit_firm_resource_idx
  on audit_logs (firm_id, resource_type, resource_id);
create index if not exists audit_firm_action_idx
  on audit_logs (firm_id, action, occurred_at desc);
create index if not exists audit_data_subject_idx
  on audit_logs (firm_id, data_subject_id) where data_subject_id is not null;

-- ------------------------------------------------------------
-- 5. email_integrations · cifrado de tokens (additive columns)
-- ------------------------------------------------------------
-- Cambiamos a versionado: nueva columna encriptada, mantenemos la vieja por
-- compatibilidad (será eliminada en Sprint 7 cuando migremos los stocks).
alter table email_integrations
  add column if not exists oauth_access_token_enc  bytea,
  add column if not exists oauth_refresh_token_enc bytea,
  add column if not exists imap_password_enc_v2    bytea,
  add column if not exists encryption_version      int default 0;

-- ------------------------------------------------------------
-- 6. paddle_webhook_events · audit log de webhooks recibidos
-- ------------------------------------------------------------
create table if not exists paddle_webhook_events (
  id                uuid primary key default gen_random_uuid(),
  event_id          text not null unique,                 -- evt_xxx de Paddle (dedup)
  event_type        text not null,                        -- 'subscription.created' | 'subscription.updated' | 'transaction.completed'
  paddle_customer_id     text,
  paddle_subscription_id text,
  firm_id           uuid references firms(id) on delete set null,
  payload           jsonb not null,
  processed         boolean default false,
  processed_at      timestamptz,
  error             text,
  received_at       timestamptz default now()
);
create index if not exists paddle_events_unprocessed_idx
  on paddle_webhook_events (received_at desc) where processed = false;
create index if not exists paddle_events_firm_idx
  on paddle_webhook_events (firm_id, received_at desc) where firm_id is not null;

-- ============================================================
-- RLS
-- ============================================================
alter table subscription_plans      enable row level security;
alter table firm_subscriptions      enable row level security;
alter table usage_events            enable row level security;
alter table audit_logs              enable row level security;
alter table paddle_webhook_events   enable row level security;

-- subscription_plans · lectura pública (catálogo)
drop policy if exists plans_select on subscription_plans;
create policy plans_select on subscription_plans for select using (true);

-- firm_subscriptions · solo la firma propia
drop policy if exists firm_subs_select on firm_subscriptions;
drop policy if exists firm_subs_modify on firm_subscriptions;
create policy firm_subs_select on firm_subscriptions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy firm_subs_modify on firm_subscriptions for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- usage_events · firma propia · service_role escribe
drop policy if exists usage_select on usage_events;
drop policy if exists usage_insert on usage_events;
create policy usage_select on usage_events for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy usage_insert on usage_events for insert
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- audit_logs · firma propia · service_role escribe
drop policy if exists audit_select on audit_logs;
drop policy if exists audit_insert on audit_logs;
create policy audit_select on audit_logs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy audit_insert on audit_logs for insert
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- paddle_webhook_events · solo service_role
drop policy if exists paddle_events_select on paddle_webhook_events;
create policy paddle_events_select on paddle_webhook_events for select
  using (auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id auto-fill + updated_at
-- ============================================================
drop trigger if exists trg_firm_subs_firm_id on firm_subscriptions;
create trigger trg_firm_subs_firm_id
  before insert on firm_subscriptions
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_firm_subs_updated_at on firm_subscriptions;
create trigger trg_firm_subs_updated_at
  before update on firm_subscriptions
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_usage_events_firm_id on usage_events;
create trigger trg_usage_events_firm_id
  before insert on usage_events
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_audit_logs_firm_id on audit_logs;
create trigger trg_audit_logs_firm_id
  before insert on audit_logs
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPC · helpers para quotas y usage
-- ============================================================

-- Devuelve el plan vigente + cuotas + uso del periodo actual de una firma.
create or replace function lexai_firm_usage(p_firm_id uuid default null)
returns jsonb
language sql
stable
as $$
  with f as (
    select coalesce(p_firm_id, auth_firm_id()) as id
  ),
  sub as (
    select s.*, p.q_users, p.q_matters, p.q_documents_mo, p.q_llm_calls_mo,
           p.q_voice_min_mo, p.q_email_accounts, p.q_judicial_subs,
           p.f_court_watcher, p.f_email_ingest, p.f_voice, p.f_canvas,
           p.f_calc, p.f_briefing, p.f_priority_support,
           p.name as plan_name, p.monthly_cop, p.annual_cop
      from firm_subscriptions s
      join subscription_plans p on p.code = s.plan_code
     where s.firm_id = (select id from f)
  ),
  period as (
    -- Periodo actual: desde el comienzo del mes calendario si no hay subscription.
    select coalesce(
      (select current_period_start from sub),
      date_trunc('month', now())
    ) as period_start
  ),
  usage as (
    select kind, sum(count) as total
      from usage_events
     where firm_id = (select id from f)
       and occurred_at >= (select period_start from period)
     group by kind
  )
  select jsonb_build_object(
    'firm_id', (select id from f),
    'plan',    coalesce((select jsonb_build_object(
                  'code', plan_code, 'name', plan_name, 'status', status,
                  'monthly_cop', monthly_cop, 'annual_cop', annual_cop,
                  'period_start', current_period_start,
                  'period_end', current_period_end
                ) from sub), jsonb_build_object('code','free','name','Free Trial','status','active')),
    'quotas',  coalesce((select jsonb_build_object(
                  'users', q_users, 'matters', q_matters,
                  'documents_mo', q_documents_mo, 'llm_calls_mo', q_llm_calls_mo,
                  'voice_min_mo', q_voice_min_mo, 'email_accounts', q_email_accounts,
                  'judicial_subs', q_judicial_subs
                ) from sub), jsonb_build_object(
                  'users',1,'matters',3,'documents_mo',20,'llm_calls_mo',200,
                  'voice_min_mo',60,'email_accounts',0,'judicial_subs',3
                )),
    'features', coalesce((select jsonb_build_object(
                  'court_watcher', f_court_watcher, 'email_ingest', f_email_ingest,
                  'voice', f_voice, 'canvas', f_canvas, 'calc', f_calc,
                  'briefing', f_briefing, 'priority_support', f_priority_support
                ) from sub), jsonb_build_object(
                  'court_watcher',true,'email_ingest',false,'voice',true,
                  'canvas',true,'calc',true,'briefing',true,'priority_support',false
                )),
    'usage',   coalesce((select jsonb_object_agg(kind, total) from usage), '{}'::jsonb),
    'period_start', (select period_start from period)
  );
$$;
