-- Fix lexai_quota_status:
--   1) Las cuotas NULL del plan (enterprise = ilimitado) se devolvían como
--      los defaults del plan free (200/60/20) por culpa de coalesce.
--   2) Ahora: si la firma SÍ tiene firm_subscriptions, respetamos el valor
--      del plan (NULL = ilimitado). Solo si no hay subscription se usan los
--      defaults del plan free.

CREATE OR REPLACE FUNCTION public.lexai_quota_status(p_firm_id uuid DEFAULT NULL::uuid)
 RETURNS jsonb
 LANGUAGE sql
 STABLE
AS $body$
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
  has_sub as (
    select exists(select 1 from sub) as h
  ),
  period as (
    select date_trunc('month', now())::date as period_start
  ),
  usage as (
    select kind, count, cost_units
      from usage_counters
     where firm_id = (select id from f)
       and period_start = (select period_start from period)
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
      -- quotas: si hay sub, respeta el plan (NULL = ilimitado).
      -- si no hay sub, usa defaults free.
      case when (select h from has_sub) then (select q_llm_calls_mo from sub) else 200 end as q_llm,
      case when (select h from has_sub) then (select q_voice_min_mo from sub) else 60  end as q_voice,
      case when (select h from has_sub) then (select q_documents_mo from sub) else 20  end as q_docs,
      case when (select h from has_sub) then (select q_matters       from sub) else 3   end as q_matters,
      case when (select h from has_sub) then (select q_users         from sub) else 1   end as q_users,
      case when (select h from has_sub) then (select q_email_accounts from sub) else 0   end as q_email,
      case when (select h from has_sub) then (select q_judicial_subs  from sub) else 3   end as q_jud,
      coalesce((select count from usage where kind = 'llm_call'), 0) as u_llm,
      coalesce((select count from usage where kind = 'voice_minute'), 0) as u_voice,
      coalesce((select count from usage where kind = 'document_upload'), 0) as u_docs,
      coalesce((select count from usage where kind = 'email_sync'), 0) as u_email,
      coalesce((select count from usage where kind = 'judicial_poll'), 0) as u_jud,
      coalesce((select f_court_watcher from sub), true)  as f_court,
      coalesce((select f_email_ingest  from sub), false) as f_email,
      coalesce((select f_voice         from sub), true)  as f_voice,
      coalesce((select f_canvas        from sub), true)  as f_canvas,
      coalesce((select f_calc          from sub), true)  as f_calc,
      coalesce((select f_briefing      from sub), true)  as f_brief,
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
      'over_llm',     case when q_llm   is null then false else u_llm   >= q_llm   end,
      'over_voice',   case when q_voice is null then false else u_voice >= q_voice end,
      'over_docs',    case when q_docs  is null then false else u_docs  >= q_docs  end,
      'near80_llm',   case when q_llm   is null then false else u_llm::float   / nullif(q_llm,0)   >= 0.8 end,
      'near80_voice', case when q_voice is null then false else u_voice::float / nullif(q_voice,0) >= 0.8 end,
      'near80_docs',  case when q_docs  is null then false else u_docs::float  / nullif(q_docs,0)  >= 0.8 end,
      'near95_llm',   case when q_llm   is null then false else u_llm::float   / nullif(q_llm,0)   >= 0.95 end,
      'near95_voice', case when q_voice is null then false else u_voice::float / nullif(q_voice,0) >= 0.95 end,
      'near95_docs',  case when q_docs  is null then false else u_docs::float  / nullif(q_docs,0)  >= 0.95 end,
      'trial_expired', case when (select status from sub) = 'trialing'
                            then coalesce((select trial_ends_at from sub),
                                          (select created_at + interval '14 days' from firms where id = (select id from f))) < now()
                            else false end
    ),
    'period_start', (select period_start from period)
  )
  from computed;
$body$;
