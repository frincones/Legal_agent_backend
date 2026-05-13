-- ============================================================
-- LexAI · Sprint 9 · Pipeline de Leads + AI Insights + Automation Rules
-- Migration date: 2026-05-10
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. lead_stages · etapas configurables del embudo por firma
-- ------------------------------------------------------------
create table if not exists lead_stages (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  sort_order        int not null default 0,
  color             text default 'blue',                    -- 'blue','green','amber','red','purple','gray'
  is_won            boolean not null default false,
  is_lost           boolean not null default false,
  created_at        timestamptz default now(),
  unique (firm_id, name)
);
create index if not exists lead_stages_firm_order_idx
  on lead_stages (firm_id, sort_order);

-- ------------------------------------------------------------
-- 2. leads · prospectos
-- ------------------------------------------------------------
create table if not exists leads (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  stage_id          uuid references lead_stages(id) on delete set null,
  nombre            text not null,
  email             text,
  telefono          text,
  source            text,                                   -- 'web','whatsapp','referido','linkedin','google_ads','otro'
  materia           text,                                   -- area legal estimada
  estimated_value_cop numeric(14,2),
  notes             text,
  score             int default 0,                          -- 0-100
  status            text not null default 'open'
    check (status in ('open','won','lost','dormant')),
  assigned_to       uuid references users(id) on delete set null,
  last_contact_at   timestamptz,
  next_followup_at  timestamptz,
  converted_client_id uuid references clients(id) on delete set null,
  converted_matter_id uuid references matters(id) on delete set null,
  converted_at      timestamptz,
  lost_reason       text,
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists leads_firm_status_idx
  on leads (firm_id, status, next_followup_at);
create index if not exists leads_firm_stage_idx
  on leads (firm_id, stage_id);
create index if not exists leads_firm_followup_idx
  on leads (firm_id, next_followup_at)
  where status = 'open' and next_followup_at is not null;

-- ------------------------------------------------------------
-- 3. lead_activities · interacciones con el lead
-- ------------------------------------------------------------
create table if not exists lead_activities (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  lead_id           uuid not null references leads(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  kind              text not null,                          -- 'note','call','email','meeting','whatsapp','stage_change'
  body              text,
  metadata          jsonb default '{}'::jsonb,
  occurred_at       timestamptz not null default now()
);
create index if not exists lead_activities_lead_idx
  on lead_activities (lead_id, occurred_at desc);

-- ------------------------------------------------------------
-- 4. ai_insights · sugerencias proactivas del agente
-- ------------------------------------------------------------
create table if not exists ai_insights (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  kind              text not null,                          -- 'missing_party','outdated_citation','deadline_unprep',
                                                            -- 'high_value_client_inactive','matter_at_risk','billing_opportunity',
                                                            -- 'lead_followup_overdue','document_missing'
  severity          text not null default 'info'
    check (severity in ('info','warning','critical')),
  target_type       text,                                   -- 'matter','client','lead','document','invoice','firm'
  target_id         uuid,
  title             text not null,
  body              text not null,
  suggested_action  text,
  action_payload    jsonb default '{}'::jsonb,              -- payload listo para ejecutar
  confidence        numeric(3,2) default 0.5,               -- 0.0-1.0
  status            text not null default 'new'
    check (status in ('new','accepted','dismissed','expired')),
  accepted_at       timestamptz,
  dismissed_at      timestamptz,
  expires_at        timestamptz,
  generated_by      text default 'llm',                     -- 'llm','rules','manual'
  metadata          jsonb default '{}'::jsonb,
  created_at        timestamptz default now()
);
create index if not exists insights_firm_status_idx
  on ai_insights (firm_id, status, created_at desc) where status = 'new';
create index if not exists insights_target_idx
  on ai_insights (firm_id, target_type, target_id);
create index if not exists insights_severity_idx
  on ai_insights (firm_id, severity, created_at desc) where severity in ('warning','critical');

-- ------------------------------------------------------------
-- 5. automation_rules · reglas tipo "Zapier" internas
-- ------------------------------------------------------------
create table if not exists automation_rules (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  description       text,
  trigger_kind      text not null,                          -- 'matter_created','matter_stage_changed','deadline_due_in',
                                                            -- 'client_created','invoice_overdue','email_received',
                                                            -- 'lead_stage_changed','schedule_daily','schedule_weekly'
  trigger_config    jsonb default '{}'::jsonb,              -- {days: 2}, {stage: 'litigio'}, etc
  conditions        jsonb default '[]'::jsonb,              -- [{field, op, value}]
  actions           jsonb not null default '[]'::jsonb,     -- [{kind, params}]
  active            boolean not null default true,
  last_run_at       timestamptz,
  last_run_status   text,
  run_count         int not null default 0,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists rules_firm_trigger_idx
  on automation_rules (firm_id, trigger_kind) where active = true;

-- ------------------------------------------------------------
-- 6. automation_runs · auditoría de cada disparo
-- ------------------------------------------------------------
create table if not exists automation_runs (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  rule_id           uuid not null references automation_rules(id) on delete cascade,
  trigger_event     text,
  trigger_payload   jsonb default '{}'::jsonb,
  actions_executed  jsonb default '[]'::jsonb,
  status            text not null default 'pending'
    check (status in ('pending','success','partial','failed','skipped')),
  error             text,
  duration_ms       int,
  started_at        timestamptz default now(),
  completed_at      timestamptz
);
create index if not exists auto_runs_rule_time_idx
  on automation_runs (rule_id, started_at desc);
create index if not exists auto_runs_firm_status_idx
  on automation_runs (firm_id, status, started_at desc) where status in ('failed','partial');

-- ============================================================
-- RLS
-- ============================================================
alter table lead_stages       enable row level security;
alter table leads             enable row level security;
alter table lead_activities   enable row level security;
alter table ai_insights       enable row level security;
alter table automation_rules  enable row level security;
alter table automation_runs   enable row level security;

drop policy if exists ls_select on lead_stages;
drop policy if exists ls_modify on lead_stages;
create policy ls_select on lead_stages for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ls_modify on lead_stages for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists leads_select on leads;
drop policy if exists leads_modify on leads;
create policy leads_select on leads for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy leads_modify on leads for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists la_select on lead_activities;
drop policy if exists la_modify on lead_activities;
create policy la_select on lead_activities for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy la_modify on lead_activities for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ai_select on ai_insights;
drop policy if exists ai_modify on ai_insights;
create policy ai_select on ai_insights for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ai_modify on ai_insights for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ar_select on automation_rules;
drop policy if exists ar_modify on automation_rules;
create policy ar_select on automation_rules for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ar_modify on automation_rules for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists auto_runs_select on automation_runs;
create policy auto_runs_select on automation_runs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers · firm_id + updated_at
-- ============================================================
drop trigger if exists trg_ls_firm_id on lead_stages;
create trigger trg_ls_firm_id before insert on lead_stages
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_leads_firm_id on leads;
create trigger trg_leads_firm_id before insert on leads
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_leads_updated_at on leads;
create trigger trg_leads_updated_at before update on leads
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_la_firm_id on lead_activities;
create trigger trg_la_firm_id before insert on lead_activities
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ai_firm_id on ai_insights;
create trigger trg_ai_firm_id before insert on ai_insights
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ar_firm_id on automation_rules;
create trigger trg_ar_firm_id before insert on automation_rules
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_ar_updated_at on automation_rules;
create trigger trg_ar_updated_at before update on automation_rules
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_aruns_firm_id on automation_runs;
create trigger trg_aruns_firm_id before insert on automation_runs
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- Default lead stages function · se llama al onboarding
-- ============================================================
create or replace function lexai_seed_default_lead_stages(p_firm_id uuid)
returns void
language plpgsql
as $$
begin
  insert into lead_stages (firm_id, name, sort_order, color, is_won, is_lost)
  values
    (p_firm_id, 'Nuevo',         10, 'blue',   false, false),
    (p_firm_id, 'Contactado',    20, 'amber',  false, false),
    (p_firm_id, 'Calificado',    30, 'purple', false, false),
    (p_firm_id, 'Propuesta',     40, 'amber',  false, false),
    (p_firm_id, 'Negociación',   50, 'amber',  false, false),
    (p_firm_id, 'Ganado',        60, 'green',  true,  false),
    (p_firm_id, 'Perdido',       70, 'red',    false, true )
  on conflict (firm_id, name) do nothing;
end;
$$;

-- Seed para todas las firmas existentes (idempotente)
do $$
declare r record;
begin
  for r in select id from firms loop
    perform lexai_seed_default_lead_stages(r.id);
  end loop;
end $$;

-- ============================================================
-- RPC · KPIs del pipeline
-- ============================================================
create or replace function lexai_pipeline_kpis(
  p_firm_id uuid default null,
  p_days    int  default 30
) returns jsonb
language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
       since as (select (now() - (p_days::text || ' days')::interval) as ts)
  select jsonb_build_object(
    'leads_total', (select count(*) from leads where firm_id = (select id from f)),
    'leads_open',  (select count(*) from leads where firm_id = (select id from f) and status = 'open'),
    'leads_won',   (select count(*) from leads where firm_id = (select id from f) and status = 'won'
                      and converted_at >= (select ts from since)),
    'leads_lost',  (select count(*) from leads where firm_id = (select id from f) and status = 'lost'
                      and updated_at >= (select ts from since)),
    'pipeline_value_cop', (select coalesce(sum(estimated_value_cop),0)
                            from leads where firm_id = (select id from f) and status = 'open'),
    'won_value_cop', (select coalesce(sum(estimated_value_cop),0)
                       from leads where firm_id = (select id from f) and status = 'won'
                         and converted_at >= (select ts from since)),
    'by_stage', coalesce((
      select jsonb_agg(jsonb_build_object(
        'stage_id', s.id, 'name', s.name, 'color', s.color,
        'count', (select count(*) from leads l where l.stage_id = s.id and l.firm_id = (select id from f) and l.status = 'open'),
        'value', (select coalesce(sum(estimated_value_cop),0) from leads l where l.stage_id = s.id and l.firm_id = (select id from f) and l.status = 'open')
      ) order by s.sort_order)
      from lead_stages s where s.firm_id = (select id from f)
    ), '[]'::jsonb),
    'overdue_followups', (select count(*) from leads
                            where firm_id = (select id from f) and status = 'open'
                              and next_followup_at is not null and next_followup_at < now())
  );
$$;
