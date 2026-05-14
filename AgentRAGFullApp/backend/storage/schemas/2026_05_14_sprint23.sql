-- ============================================================
-- LexAI · Sprint 23 · M36 Paddle Billing + Free Plan Auto-Onboard
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP · NO ALTER on existing columns
-- ============================================================
-- Depends on: Sprint 6 (subscription_plans, firm_subscriptions, usage_events)
-- ============================================================

-- ------------------------------------------------------------
-- 1. usage_counters · cache mensual por (firm_id, period_start, kind)
--    para queries de cuota O(1) sin scan de usage_events.
-- ------------------------------------------------------------
create table if not exists usage_counters (
  firm_id       uuid not null references firms(id) on delete cascade,
  period_start  date not null,                -- primer día del mes calendario
  kind          text not null,                -- 'llm_call' | 'voice_minute' | ...
  count         bigint not null default 0,
  cost_units    numeric(14,4) not null default 0,
  updated_at    timestamptz not null default now(),
  primary key (firm_id, period_start, kind)
);
create index if not exists usage_counters_firm_period_idx
  on usage_counters (firm_id, period_start desc);

alter table usage_counters enable row level security;

drop policy if exists usage_counters_select on usage_counters;
drop policy if exists usage_counters_modify on usage_counters;
create policy usage_counters_select on usage_counters for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy usage_counters_modify on usage_counters for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 2. RPC · lexai_increment_usage
--    Inserta evento granular + actualiza el counter atómicamente.
-- ------------------------------------------------------------
create or replace function lexai_increment_usage(
  p_firm_id uuid,
  p_user_id uuid,
  p_kind    text,
  p_count   int  default 1,
  p_cost    numeric default 0,
  p_meta    jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  v_period date := date_trunc('month', now())::date;
begin
  insert into usage_events (firm_id, user_id, kind, count, cost_units, metadata)
  values (p_firm_id, p_user_id, p_kind, p_count, p_cost, p_meta);

  insert into usage_counters (firm_id, period_start, kind, count, cost_units, updated_at)
  values (p_firm_id, v_period, p_kind, p_count, p_cost, now())
  on conflict (firm_id, period_start, kind) do update
    set count = usage_counters.count + excluded.count,
        cost_units = usage_counters.cost_units + excluded.cost_units,
        updated_at = now();
end;
$$;

grant execute on function lexai_increment_usage(uuid, uuid, text, int, numeric, jsonb) to authenticated, service_role;

-- ------------------------------------------------------------
-- 3. RPC · lexai_quota_status
--    Devuelve estado normalizado: plan + cuotas + uso + flags
--    (over_quota_kinds, near_80, near_95) para QuotaBanner + tools.
-- ------------------------------------------------------------
create or replace function lexai_quota_status(p_firm_id uuid default null)
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
    select date_trunc('month', now())::date as period_start
  ),
  usage as (
    -- Lee del counter cache (rápido)
    select kind, count, cost_units
      from usage_counters
     where firm_id = (select id from f)
       and period_start = (select period_start from period)
  ),
  usage_map as (
    select coalesce(jsonb_object_agg(kind, count), '{}'::jsonb) as u
      from usage
  ),
  computed as (
    select
      coalesce((select plan_code from sub), 'free') as plan_code,
      coalesce((select plan_name from sub), 'Free Trial') as plan_name,
      coalesce((select status from sub), 'trialing') as status,
      coalesce((select trial_ends_at from sub),
               (select created_at + interval '14 days' from firms where id = (select id from f))) as trial_ends_at,
      (select monthly_cop from sub) as monthly_cop,
      (select annual_cop from sub) as annual_cop,
      (select current_period_start from sub) as period_start_sub,
      (select current_period_end from sub) as period_end_sub,
      -- quotas (null = unlimited)
      coalesce((select q_llm_calls_mo from sub), 200) as q_llm,
      coalesce((select q_voice_min_mo from sub), 60) as q_voice,
      coalesce((select q_documents_mo from sub), 20) as q_docs,
      coalesce((select q_matters from sub), 3) as q_matters,
      coalesce((select q_users from sub), 1) as q_users,
      coalesce((select q_email_accounts from sub), 0) as q_email,
      coalesce((select q_judicial_subs from sub), 3) as q_jud,
      -- usage
      coalesce((select count from usage where kind = 'llm_call'), 0) as u_llm,
      coalesce((select count from usage where kind = 'voice_minute'), 0) as u_voice,
      coalesce((select count from usage where kind = 'document_upload'), 0) as u_docs,
      coalesce((select count from usage where kind = 'email_sync'), 0) as u_email,
      coalesce((select count from usage where kind = 'judicial_poll'), 0) as u_jud,
      -- features
      coalesce((select f_court_watcher from sub), true) as f_court,
      coalesce((select f_email_ingest from sub), false) as f_email,
      coalesce((select f_voice from sub), true) as f_voice,
      coalesce((select f_canvas from sub), true) as f_canvas,
      coalesce((select f_calc from sub), true) as f_calc,
      coalesce((select f_briefing from sub), true) as f_brief,
      coalesce((select f_priority_support from sub), false) as f_priority
  )
  select jsonb_build_object(
    'firm_id', (select id from f),
    'plan', jsonb_build_object(
      'code', plan_code,
      'name', plan_name,
      'status', status,
      'trial_ends_at', trial_ends_at,
      'monthly_cop', monthly_cop,
      'annual_cop', annual_cop,
      'period_start', coalesce(period_start_sub, (select period_start from period)::timestamptz),
      'period_end', period_end_sub
    ),
    'quotas', jsonb_build_object(
      'llm_calls_mo', q_llm,
      'voice_min_mo', q_voice,
      'documents_mo', q_docs,
      'matters', q_matters,
      'users', q_users,
      'email_accounts', q_email,
      'judicial_subs', q_jud
    ),
    'usage', jsonb_build_object(
      'llm_call', u_llm,
      'voice_minute', u_voice,
      'document_upload', u_docs,
      'email_sync', u_email,
      'judicial_poll', u_jud
    ),
    'features', jsonb_build_object(
      'court_watcher', f_court,
      'email_ingest', f_email,
      'voice', f_voice,
      'canvas', f_canvas,
      'calc', f_calc,
      'briefing', f_brief,
      'priority_support', f_priority
    ),
    'flags', jsonb_build_object(
      'over_llm', case when q_llm is null then false else u_llm >= q_llm end,
      'over_voice', case when q_voice is null then false else u_voice >= q_voice end,
      'over_docs', case when q_docs is null then false else u_docs >= q_docs end,
      'near80_llm', case when q_llm is null then false else u_llm::float / nullif(q_llm,0) >= 0.8 end,
      'near80_voice', case when q_voice is null then false else u_voice::float / nullif(q_voice,0) >= 0.8 end,
      'near80_docs', case when q_docs is null then false else u_docs::float / nullif(q_docs,0) >= 0.8 end,
      'near95_llm', case when q_llm is null then false else u_llm::float / nullif(q_llm,0) >= 0.95 end,
      'near95_voice', case when q_voice is null then false else u_voice::float / nullif(q_voice,0) >= 0.95 end,
      'near95_docs', case when q_docs is null then false else u_docs::float / nullif(q_docs,0) >= 0.95 end,
      'trial_expired', case when (select status from sub) = 'trialing'
                            then coalesce((select trial_ends_at from sub),
                                          (select created_at + interval '14 days' from firms where id = (select id from f))) < now()
                            else false end
    ),
    'period_start', (select period_start from period)
  )
  from computed;
$$;

grant execute on function lexai_quota_status(uuid) to authenticated, service_role;

-- ------------------------------------------------------------
-- 4. RPC · lexai_check_quota
--    Boolean rápido para precheck de herramientas (voice tools).
-- ------------------------------------------------------------
create or replace function lexai_check_quota(
  p_firm_id uuid,
  p_kind    text,
  p_amount  int default 1
)
returns jsonb
language plpgsql
stable
as $$
declare
  v_period date := date_trunc('month', now())::date;
  v_plan   text;
  v_status text;
  v_quota  int;
  v_used   bigint;
  v_col    text;
begin
  select plan_code, status into v_plan, v_status
    from firm_subscriptions where firm_id = p_firm_id;
  if v_plan is null then
    v_plan := 'free';
    v_status := 'trialing';
  end if;

  -- map kind → quota column
  v_col := case p_kind
    when 'llm_call' then 'q_llm_calls_mo'
    when 'voice_minute' then 'q_voice_min_mo'
    when 'document_upload' then 'q_documents_mo'
    when 'email_sync' then 'q_email_accounts'
    when 'judicial_poll' then 'q_judicial_subs'
    else null
  end;

  if v_col is null then
    return jsonb_build_object('ok', true, 'reason', 'unknown_kind');
  end if;

  execute format('select %I from subscription_plans where code = $1', v_col)
    into v_quota using v_plan;

  if v_quota is null then
    return jsonb_build_object('ok', true, 'reason', 'unlimited', 'plan', v_plan);
  end if;

  select coalesce(count, 0) into v_used
    from usage_counters
   where firm_id = p_firm_id and period_start = v_period and kind = p_kind;

  return jsonb_build_object(
    'ok', (v_used + p_amount) <= v_quota,
    'plan', v_plan,
    'status', v_status,
    'quota', v_quota,
    'used', v_used,
    'remaining', greatest(0, v_quota - v_used),
    'requested', p_amount,
    'period_start', v_period
  );
end;
$$;

grant execute on function lexai_check_quota(uuid, text, int) to authenticated, service_role;

-- ------------------------------------------------------------
-- 5. Trigger · auto-asignar plan free al crear firm
--    Crea firm_subscriptions row con plan='free', status='trialing',
--    trial_ends_at = created_at + 14 días. Idempotente (do nothing on conflict).
-- ------------------------------------------------------------
create or replace function lexai_auto_assign_free_plan()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into firm_subscriptions
    (firm_id, plan_code, status, trial_ends_at,
     current_period_start, current_period_end)
  values
    (new.id, 'free', 'trialing', now() + interval '14 days',
     date_trunc('month', now()),
     date_trunc('month', now()) + interval '1 month')
  on conflict (firm_id) do nothing;
  return new;
end;
$$;

drop trigger if exists trg_firms_auto_assign_free on firms;
create trigger trg_firms_auto_assign_free
  after insert on firms
  for each row execute function lexai_auto_assign_free_plan();

-- Backfill: cualquier firma existente sin subscription → free trialing
insert into firm_subscriptions
  (firm_id, plan_code, status, trial_ends_at,
   current_period_start, current_period_end)
select f.id, 'free', 'trialing', now() + interval '14 days',
       date_trunc('month', now()),
       date_trunc('month', now()) + interval '1 month'
  from firms f
 where not exists (select 1 from firm_subscriptions s where s.firm_id = f.id)
on conflict (firm_id) do nothing;

-- ------------------------------------------------------------
-- 6. Backfill usage_counters desde usage_events del mes actual
--    (one-shot · idempotente: si la row ya existe, no la pisamos).
-- ------------------------------------------------------------
insert into usage_counters (firm_id, period_start, kind, count, cost_units, updated_at)
select firm_id,
       date_trunc('month', occurred_at)::date as period_start,
       kind,
       sum(count) as count,
       sum(cost_units) as cost_units,
       max(occurred_at) as updated_at
  from usage_events
 where occurred_at >= date_trunc('month', now())
 group by firm_id, date_trunc('month', occurred_at)::date, kind
on conflict (firm_id, period_start, kind) do nothing;

-- ------------------------------------------------------------
-- 7. Vista admin · firm_billing_overview (para panel SaaS admin)
-- ------------------------------------------------------------
create or replace view firm_billing_overview as
  select
    s.firm_id,
    f.razon_social,
    f.country,
    f.created_at as firm_created_at,
    s.plan_code,
    p.name as plan_name,
    s.status,
    s.billing_period,
    s.current_period_start,
    s.current_period_end,
    s.trial_ends_at,
    s.canceled_at,
    s.paddle_subscription_id,
    p.monthly_cop,
    p.annual_cop,
    coalesce((select count from usage_counters c
              where c.firm_id = s.firm_id
                and c.period_start = date_trunc('month', now())::date
                and c.kind = 'llm_call'), 0) as llm_calls_mtd,
    coalesce((select count from usage_counters c
              where c.firm_id = s.firm_id
                and c.period_start = date_trunc('month', now())::date
                and c.kind = 'voice_minute'), 0) as voice_min_mtd,
    coalesce((select count from usage_counters c
              where c.firm_id = s.firm_id
                and c.period_start = date_trunc('month', now())::date
                and c.kind = 'document_upload'), 0) as documents_mtd
  from firm_subscriptions s
  join firms f on f.id = s.firm_id
  join subscription_plans p on p.code = s.plan_code;

grant select on firm_billing_overview to authenticated, service_role;

-- ============================================================
-- Done · Sprint 23 migration
-- ============================================================
