-- ============================================================
-- LexAI · Sprint 24 · SaaS Admin Panel completo
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP · NO ALTER on existing columns
-- ============================================================
-- Depends on: lexai_multi_tenant_migration.sql (firms, users, audit_logs),
--             Sprint 6 (firm_subscriptions, subscription_plans),
--             Sprint 23 (usage_counters, firm_billing_overview).
-- ============================================================

-- ------------------------------------------------------------
-- 1. admin_users · SaaS owners independientes de firms
--    Identificados por su Supabase auth user_id (auth.users.id).
--    Sin RLS firm-scoping: solo service_role + el propio admin pueden leer.
-- ------------------------------------------------------------
create table if not exists admin_users (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique not null,                 -- = auth.users.id de Supabase
  email         text unique not null,
  full_name     text,
  role          text not null default 'admin'
    check (role in ('owner','admin','support','readonly')),
  active        boolean not null default true,
  last_login_at timestamptz,
  created_at    timestamptz default now(),
  created_by    uuid references admin_users(id),
  metadata      jsonb default '{}'::jsonb
);
create index if not exists admin_users_email_idx on admin_users (email);
create index if not exists admin_users_active_idx on admin_users (active) where active = true;

alter table admin_users enable row level security;
drop policy if exists admin_users_select on admin_users;
drop policy if exists admin_users_modify on admin_users;
-- Un admin puede leerse a sí mismo · service_role lee todo
create policy admin_users_select on admin_users for select
  using (auth.role() = 'service_role' or auth_user_id = auth.uid());
create policy admin_users_modify on admin_users for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- Helper · ¿el JWT actual corresponde a un admin SaaS?
create or replace function is_saas_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from admin_users
     where auth_user_id = auth.uid()
       and active = true
  );
$$;
grant execute on function is_saas_admin() to authenticated, service_role;

-- ------------------------------------------------------------
-- 2. feature_flags · catálogo global de feature flags
-- ------------------------------------------------------------
create table if not exists feature_flags (
  key            text primary key,                    -- 'court_watcher_v2' | 'new_dashboard'
  name           text not null,
  description    text,
  category       text default 'general',              -- 'experimental' | 'beta' | 'gated' | 'kill_switch'
  default_value  boolean not null default false,
  rollout_pct    int default 0 check (rollout_pct between 0 and 100),
  created_at     timestamptz default now(),
  updated_at     timestamptz default now()
);
create index if not exists feature_flags_category_idx on feature_flags (category);

alter table feature_flags enable row level security;
drop policy if exists feature_flags_select on feature_flags;
drop policy if exists feature_flags_modify on feature_flags;
create policy feature_flags_select on feature_flags for select using (true);
create policy feature_flags_modify on feature_flags for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. firm_feature_overrides · override por firma
-- ------------------------------------------------------------
create table if not exists firm_feature_overrides (
  firm_id     uuid not null references firms(id) on delete cascade,
  flag_key    text not null references feature_flags(key) on delete cascade,
  enabled     boolean not null,
  reason      text,
  expires_at  timestamptz,
  created_by  uuid references admin_users(id),
  created_at  timestamptz default now(),
  primary key (firm_id, flag_key)
);
create index if not exists firm_feature_overrides_firm_idx on firm_feature_overrides (firm_id);

alter table firm_feature_overrides enable row level security;
drop policy if exists firm_feature_overrides_select on firm_feature_overrides;
drop policy if exists firm_feature_overrides_modify on firm_feature_overrides;
create policy firm_feature_overrides_select on firm_feature_overrides for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy firm_feature_overrides_modify on firm_feature_overrides for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- RPC · evaluar flag para una firma (cliente o admin)
create or replace function lexai_feature_enabled(p_firm_id uuid, p_flag_key text)
returns boolean
language sql
stable
as $$
  with override as (
    select enabled from firm_feature_overrides
     where firm_id = p_firm_id and flag_key = p_flag_key
       and (expires_at is null or expires_at > now())
     limit 1
  ),
  flag as (
    select default_value, rollout_pct from feature_flags where key = p_flag_key
  )
  select coalesce(
    (select enabled from override),
    case
      when (select rollout_pct from flag) >= 100 then true
      when (select rollout_pct from flag) <= 0 then (select default_value from flag)
      -- rollout determinista por hash del firm_id
      when ('x' || substr(md5(p_firm_id::text), 1, 8))::bit(32)::int % 100
           < (select rollout_pct from flag)
        then true
      else (select default_value from flag)
    end,
    false
  );
$$;
grant execute on function lexai_feature_enabled(uuid, text) to authenticated, service_role;

-- ------------------------------------------------------------
-- 4. support_tickets · cola de soporte SaaS
-- ------------------------------------------------------------
create table if not exists support_tickets (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid references firms(id) on delete set null,
  user_id         uuid references users(id) on delete set null,
  reporter_email  text not null,
  subject         text not null,
  body            text not null,
  category        text default 'general'
    check (category in ('general','bug','billing','feature_request','account','onboarding')),
  status          text not null default 'open'
    check (status in ('open','in_progress','waiting_user','resolved','closed')),
  priority        text not null default 'normal'
    check (priority in ('low','normal','high','urgent')),
  assigned_to     uuid references admin_users(id),
  resolved_at     timestamptz,
  closed_at       timestamptz,
  metadata        jsonb default '{}'::jsonb,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists support_tickets_status_idx on support_tickets (status, priority desc, created_at desc);
create index if not exists support_tickets_firm_idx on support_tickets (firm_id, created_at desc);
create index if not exists support_tickets_assigned_idx on support_tickets (assigned_to) where assigned_to is not null;

create table if not exists support_ticket_messages (
  id              uuid primary key default gen_random_uuid(),
  ticket_id       uuid not null references support_tickets(id) on delete cascade,
  author_kind     text not null check (author_kind in ('reporter','admin')),
  admin_user_id   uuid references admin_users(id),
  user_id         uuid references users(id),
  body            text not null,
  internal_note   boolean default false,
  created_at      timestamptz default now()
);
create index if not exists support_ticket_messages_ticket_idx on support_ticket_messages (ticket_id, created_at);

alter table support_tickets enable row level security;
alter table support_ticket_messages enable row level security;

drop policy if exists support_tickets_select on support_tickets;
drop policy if exists support_tickets_modify on support_tickets;
create policy support_tickets_select on support_tickets for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy support_tickets_modify on support_tickets for all
  using (is_saas_admin() or auth.role() = 'service_role'
         or (firm_id = auth_firm_id() and status = 'open'))
  with check (is_saas_admin() or auth.role() = 'service_role'
              or firm_id = auth_firm_id());

drop policy if exists support_tickets_msg_select on support_ticket_messages;
drop policy if exists support_tickets_msg_modify on support_ticket_messages;
create policy support_tickets_msg_select on support_ticket_messages for select
  using (
    exists (select 1 from support_tickets t
             where t.id = ticket_id
               and (t.firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role'))
    and (not internal_note or is_saas_admin() or auth.role() = 'service_role')
  );
create policy support_tickets_msg_modify on support_ticket_messages for all
  using (is_saas_admin() or auth.role() = 'service_role'
         or exists (select 1 from support_tickets t where t.id = ticket_id and t.firm_id = auth_firm_id()))
  with check (is_saas_admin() or auth.role() = 'service_role'
              or exists (select 1 from support_tickets t where t.id = ticket_id and t.firm_id = auth_firm_id()));

-- Trigger · updated_at
drop trigger if exists trg_support_tickets_updated_at on support_tickets;
create trigger trg_support_tickets_updated_at
  before update on support_tickets
  for each row execute function tg_set_updated_at();

-- ------------------------------------------------------------
-- 5. RPCs · métricas SaaS para dashboard admin
-- ------------------------------------------------------------

-- MRR = suma del precio mensual de todas las subs active/trialing (trialing = 0 COP)
create or replace function lexai_saas_mrr()
returns jsonb
language sql
stable
as $$
  with active_subs as (
    select s.firm_id, s.plan_code, s.billing_period, s.status, p.monthly_cop, p.annual_cop
      from firm_subscriptions s
      join subscription_plans p on p.code = s.plan_code
     where s.status in ('active','trialing','past_due')
  ),
  calc as (
    select
      count(*) filter (where status = 'active') as paying,
      count(*) filter (where status = 'trialing') as trialing,
      count(*) filter (where status = 'past_due') as past_due,
      coalesce(sum(case
        when status = 'active' and billing_period = 'monthly' then monthly_cop
        when status = 'active' and billing_period = 'annual'  then annual_cop / 12
        else 0
      end), 0)::bigint as mrr_cop,
      coalesce(sum(case
        when status = 'active' and billing_period = 'annual' then annual_cop
        when status = 'active' and billing_period = 'monthly' then monthly_cop * 12
        else 0
      end), 0)::bigint as arr_cop
      from active_subs
  )
  select jsonb_build_object(
    'mrr_cop', mrr_cop,
    'arr_cop', arr_cop,
    'paying_firms', paying,
    'trialing_firms', trialing,
    'past_due_firms', past_due,
    'arpu_cop', case when paying > 0 then mrr_cop / paying else 0 end
  ) from calc;
$$;
grant execute on function lexai_saas_mrr() to authenticated, service_role;

-- Signups MTD + crecimiento
create or replace function lexai_saas_signups_mtd()
returns jsonb
language sql
stable
as $$
  with mtd as (
    select count(*) as signups_mtd
      from firms
     where created_at >= date_trunc('month', now())
  ),
  prev as (
    select count(*) as signups_prev
      from firms
     where created_at >= date_trunc('month', now() - interval '1 month')
       and created_at < date_trunc('month', now())
  ),
  total as (
    select count(*) as total_firms from firms
  )
  select jsonb_build_object(
    'signups_mtd', (select signups_mtd from mtd),
    'signups_prev_month', (select signups_prev from prev),
    'total_firms', (select total_firms from total),
    'growth_pct', case
      when (select signups_prev from prev) > 0
      then round(100.0 * ((select signups_mtd from mtd) - (select signups_prev from prev))
                 / (select signups_prev from prev), 1)
      else null
    end
  );
$$;
grant execute on function lexai_saas_signups_mtd() to authenticated, service_role;

-- Churn rolling 30d (firms con status='canceled' o trial_expired sin upgrade)
create or replace function lexai_saas_churn_30d()
returns jsonb
language sql
stable
as $$
  with churned as (
    select count(*) as n
      from firm_subscriptions
     where status = 'canceled'
       and canceled_at >= now() - interval '30 days'
  ),
  base as (
    select count(*) as n
      from firm_subscriptions
     where status in ('active','past_due')
        or (status = 'canceled' and canceled_at >= now() - interval '30 days')
  )
  select jsonb_build_object(
    'churned_30d', (select n from churned),
    'base_30d', (select n from base),
    'churn_rate_pct', case when (select n from base) > 0
                           then round(100.0 * (select n from churned)::numeric / (select n from base), 1)
                           else 0 end
  );
$$;
grant execute on function lexai_saas_churn_30d() to authenticated, service_role;

-- Salud de una firm (resumen)
create or replace function lexai_saas_firm_health(p_firm_id uuid)
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'firm_id', p_firm_id,
    'razon_social', (select razon_social from firms where id = p_firm_id),
    'country', (select country from firms where id = p_firm_id),
    'created_at', (select created_at from firms where id = p_firm_id),
    'plan', (select jsonb_build_object('code', plan_code, 'status', status,
                                       'trial_ends_at', trial_ends_at)
               from firm_subscriptions where firm_id = p_firm_id),
    'users_count', (select count(*) from users where firm_id = p_firm_id),
    'matters_total', (select count(*) from matters where firm_id = p_firm_id),
    'matters_active', (select count(*) from matters where firm_id = p_firm_id and status = 'activo'),
    'open_tickets', (select count(*) from support_tickets where firm_id = p_firm_id and status not in ('resolved','closed')),
    'llm_calls_mtd', coalesce(
      (select count from usage_counters
        where firm_id = p_firm_id
          and period_start = date_trunc('month', now())::date
          and kind = 'llm_call'), 0),
    'voice_min_mtd', coalesce(
      (select count from usage_counters
        where firm_id = p_firm_id
          and period_start = date_trunc('month', now())::date
          and kind = 'voice_minute'), 0)
  );
$$;
grant execute on function lexai_saas_firm_health(uuid) to authenticated, service_role;

-- ------------------------------------------------------------
-- 6. Vista admin · admin_audit_recent (últimas N acciones admin)
--    Reusa audit_logs filtrando por metadata.scope='saas_admin'.
-- ------------------------------------------------------------
create or replace view admin_audit_recent as
  select id, firm_id, user_id, action, resource_type, resource_id,
         outcome, reason, occurred_at, metadata
    from audit_logs
   where (metadata->>'scope') = 'saas_admin'
   order by occurred_at desc;
grant select on admin_audit_recent to authenticated, service_role;

-- ------------------------------------------------------------
-- 7. Seed inicial · feature flags básicos
-- ------------------------------------------------------------
insert into feature_flags (key, name, description, category, default_value, rollout_pct)
values
  ('court_watcher_v2', 'Court Watcher v2 (RPA)', 'Polling judicial mejorado con scraping', 'beta', false, 0),
  ('voice_agent_v2', 'Voice Agent v2', 'Nuevo agente de voz con interrupción y barge-in', 'experimental', false, 25),
  ('canvas_streaming', 'Canvas streaming', 'Streaming de generación de docs en canvas', 'beta', true, 100),
  ('mobile_pwa', 'Mobile PWA', 'Instalación PWA en mobile', 'general', true, 100),
  ('predictive_analytics', 'Analytics predictivos', 'Forecasting de cartera + churn', 'beta', false, 0),
  ('client_portal_v2', 'Portal cliente v2', 'Nueva UI del portal cliente B2C', 'experimental', false, 10)
on conflict (key) do nothing;

-- ============================================================
-- Done · Sprint 24 migration
-- ============================================================
