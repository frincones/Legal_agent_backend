-- ============================================================
-- LexAI · Sprint 18 · Analytics v2 + Executive Dashboards
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Aporte:
--   1. firm_analytics_snapshots · KPIs congelados por día/firm (trend histórico)
--   2. report_definitions       · reports personalizados por usuario
--   3. report_runs              · cache de ejecuciones de reports
--   4. RPCs ejecutivos: revenue, performance, pipeline, prediction_accuracy
--   5. RLS firm-scoped + índices
-- ============================================================

-- ------------------------------------------------------------
-- 1. firm_analytics_snapshots
--    Una fila por (firm_id, snapshot_date). Diaria. Idempotente:
--    el worker hace UPSERT, así corre sin riesgo varias veces.
-- ------------------------------------------------------------
create table if not exists firm_analytics_snapshots (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  snapshot_date     date not null,
  -- Casos
  matters_total     int not null default 0,
  matters_active    int not null default 0,
  matters_closed    int not null default 0,
  matters_won       int not null default 0,
  matters_lost      int not null default 0,
  -- Tiempo
  billable_minutes_total      bigint not null default 0,
  billable_minutes_30d        bigint not null default 0,
  non_billable_minutes_30d    bigint not null default 0,
  -- Facturación (mes calendario)
  invoiced_cop_mtd  numeric(16,2) not null default 0,            -- emitido este mes
  collected_cop_mtd numeric(16,2) not null default 0,            -- cobrado este mes
  ar_total_cop      numeric(16,2) not null default 0,            -- por cobrar pendiente
  ar_overdue_cop    numeric(16,2) not null default 0,            -- vencido
  -- Pipeline
  leads_open        int not null default 0,
  leads_won_30d     int not null default 0,
  leads_lost_30d    int not null default 0,
  -- Predicciones IA
  predictions_30d   int not null default 0,
  predictions_reviewed_30d int not null default 0,
  -- Tareas (Sprint 17)
  tasks_open        int not null default 0,
  tasks_overdue     int not null default 0,
  -- Colaboración (Sprint 16)
  comments_30d      int not null default 0,
  -- KB (Sprint 15)
  kb_entries_total  int not null default 0,
  lessons_total     int not null default 0,
  -- Meta
  payload           jsonb default '{}'::jsonb,                   -- breakdowns extra
  computed_at       timestamptz default now(),
  unique (firm_id, snapshot_date)
);
create index if not exists fas_firm_date_idx
  on firm_analytics_snapshots (firm_id, snapshot_date desc);

-- ------------------------------------------------------------
-- 2. report_definitions · reports configurables guardados
-- ------------------------------------------------------------
create table if not exists report_definitions (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  name              text not null,
  description       text,
  scope             text not null
    check (scope in ('revenue','performance','pipeline','predictions','matters','time','custom')),
  config            jsonb not null default '{}'::jsonb,          -- {metrics:[], group_by:'', filters:{}, period:''}
  shared_with_firm  boolean not null default false,              -- visible para todos los del firm
  pinned            boolean not null default false,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  unique (user_id, name)
);
create index if not exists rdef_firm_scope_idx
  on report_definitions (firm_id, scope, pinned desc);

-- ------------------------------------------------------------
-- 3. report_runs · cache + audit trail de ejecuciones
-- ------------------------------------------------------------
create table if not exists report_runs (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  report_id         uuid references report_definitions(id) on delete cascade,
  user_id           uuid references users(id) on delete set null,
  params_hash       text,                                        -- hash de los params para reusar cache
  result            jsonb,
  row_count         int default 0,
  duration_ms       int default 0,
  status            text not null default 'ok'
    check (status in ('ok','failed','partial')),
  error             text,
  ran_at            timestamptz default now()
);
create index if not exists rruns_firm_idx
  on report_runs (firm_id, ran_at desc);
create index if not exists rruns_report_idx
  on report_runs (report_id, ran_at desc) where report_id is not null;

-- ============================================================
-- RLS
-- ============================================================
alter table firm_analytics_snapshots enable row level security;
alter table report_definitions       enable row level security;
alter table report_runs              enable row level security;

drop policy if exists fas_select on firm_analytics_snapshots;
drop policy if exists fas_modify on firm_analytics_snapshots;
create policy fas_select on firm_analytics_snapshots for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy fas_modify on firm_analytics_snapshots for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists rdef_select on report_definitions;
drop policy if exists rdef_modify on report_definitions;
create policy rdef_select on report_definitions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy rdef_modify on report_definitions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists rruns_select on report_runs;
drop policy if exists rruns_modify on report_runs;
create policy rruns_select on report_runs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy rruns_modify on report_runs for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_fas_firm_id on firm_analytics_snapshots;
create trigger trg_fas_firm_id before insert on firm_analytics_snapshots
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_rdef_firm_id on report_definitions;
create trigger trg_rdef_firm_id before insert on report_definitions
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_rdef_updated_at on report_definitions;
create trigger trg_rdef_updated_at before update on report_definitions
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_rruns_firm_id on report_runs;
create trigger trg_rruns_firm_id before insert on report_runs
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- RPCs · ejecutivos
-- ============================================================

-- Revenue trend mensual (últimos N meses)
create or replace function lexai_revenue_trend(
  p_firm_id  uuid,
  p_months   int default 12
) returns table (
  month_start date,
  invoiced_cop numeric,
  collected_cop numeric,
  outstanding_cop numeric,
  invoices_count int
)
language sql stable as $$
  with months as (
    select generate_series(
      date_trunc('month', now() - make_interval(months => p_months - 1))::date,
      date_trunc('month', now())::date,
      interval '1 month'
    )::date as month_start
  )
  select
    m.month_start,
    coalesce((
      select sum(i.total_cop) from invoices i
       where i.firm_id = p_firm_id
         and i.status not in ('draft','void')
         and date_trunc('month', i.created_at)::date = m.month_start
    ), 0) as invoiced_cop,
    coalesce((
      select sum(i.paid_amount_cop) from invoices i
       where i.firm_id = p_firm_id
         and i.paid_at is not null
         and date_trunc('month', i.paid_at)::date = m.month_start
    ), 0) as collected_cop,
    coalesce((
      select sum(i.total_cop - i.paid_amount_cop) from invoices i
       where i.firm_id = p_firm_id
         and i.status in ('sent','partially_paid','overdue')
         and date_trunc('month', i.created_at)::date = m.month_start
    ), 0) as outstanding_cop,
    coalesce((
      select count(*)::int from invoices i
       where i.firm_id = p_firm_id
         and i.status not in ('draft','void')
         and date_trunc('month', i.created_at)::date = m.month_start
    ), 0) as invoices_count
  from months m
  order by m.month_start;
$$;

-- AR aging (buckets actuales)
create or replace function lexai_ar_aging(p_firm_id uuid)
returns jsonb language sql stable as $$
  with aged as (
    select
      case
        when due_date is null then 'no_due'
        when due_date >= current_date then 'current'
        when due_date >= current_date - interval '30 days' then 'd1_30'
        when due_date >= current_date - interval '60 days' then 'd31_60'
        when due_date >= current_date - interval '90 days' then 'd61_90'
        else 'd90_plus'
      end as bucket,
      total_cop - paid_amount_cop as balance,
      count(*) over () as total_count
      from invoices
     where firm_id = p_firm_id
       and status in ('sent','partially_paid','overdue')
       and (total_cop - paid_amount_cop) > 0
  )
  select coalesce(jsonb_object_agg(bucket, jsonb_build_object('amount', amount, 'count', cnt)), '{}'::jsonb)
    from (
      select bucket, sum(balance) as amount, count(*)::int as cnt
        from aged
       group by bucket
    ) g;
$$;

-- Performance por abogado (últimos N días)
create or replace function lexai_lawyer_performance(
  p_firm_id  uuid,
  p_days     int default 30
) returns table (
  user_id uuid,
  full_name text,
  avatar_url text,
  billable_minutes bigint,
  non_billable_minutes bigint,
  matters_count int,
  invoiced_cop numeric,
  tasks_completed int
)
language sql stable as $$
  with win as (
    select now() - make_interval(days => p_days) as since
  )
  select
    u.id as user_id,
    u.full_name,
    u.avatar_url,
    coalesce(sum(te.duration_min) filter (
      where te.billable = true and te.ended_at is not null
        and te.ended_at >= (select since from win)
    ), 0) as billable_minutes,
    coalesce(sum(te.duration_min) filter (
      where te.billable = false and te.ended_at is not null
        and te.ended_at >= (select since from win)
    ), 0) as non_billable_minutes,
    (select count(distinct m.id)::int from matters m
       where m.firm_id = p_firm_id and m.owner_user_id = u.id) as matters_count,
    coalesce((
      select sum(il.total_cop) from invoice_lines il
        join time_entries te2 on te2.id = il.time_entry_id
       where te2.user_id = u.id
         and te2.ended_at >= (select since from win)
    ), 0) as invoiced_cop,
    coalesce((
      select count(*)::int from tasks t
       where t.firm_id = p_firm_id and t.completed_by = u.id
         and t.completed_at >= (select since from win)
    ), 0) as tasks_completed
  from users u
   left join time_entries te on te.user_id = u.id and te.firm_id = p_firm_id
  where u.firm_id = p_firm_id
  group by u.id, u.full_name, u.avatar_url
  order by billable_minutes desc;
$$;

-- Pipeline · leads → matters → outcome
create or replace function lexai_pipeline_funnel(
  p_firm_id  uuid,
  p_days     int default 90
) returns table (
  stage text,
  count int,
  amount_cop numeric
)
language sql stable as $$
  with win as (
    select now() - make_interval(days => p_days) as since
  )
  select 'leads_open'::text as stage,
         count(*)::int as count,
         coalesce(sum(estimated_value_cop), 0)::numeric as amount_cop
    from leads
   where firm_id = p_firm_id
     and status = 'open'
     and created_at >= (select since from win)
  union all
  select 'leads_won'::text,
         count(*)::int,
         coalesce(sum(estimated_value_cop), 0)::numeric
    from leads
   where firm_id = p_firm_id and status = 'won'
     and updated_at >= (select since from win)
  union all
  select 'matters_active'::text,
         count(*)::int,
         coalesce(sum(cuantia), 0)::numeric
    from matters
   where firm_id = p_firm_id and status = 'activo'
     and created_at >= (select since from win)
  union all
  select 'matters_closed'::text,
         count(*)::int,
         coalesce(sum(cuantia), 0)::numeric
    from matters
   where firm_id = p_firm_id and status in ('cerrado','archivado')
     and updated_at >= (select since from win);
$$;

-- Accuracy de predicciones: compara predicción IA vs outcome real de case_lessons
create or replace function lexai_prediction_accuracy(
  p_firm_id uuid,
  p_days    int default 180
) returns jsonb
language plpgsql stable as $$
declare
  v_window timestamptz := now() - make_interval(days => p_days);
  v_total int;
  v_correct int;
  v_by_outcome jsonb;
  v_avg_confidence_correct real;
  v_avg_confidence_wrong real;
begin
  -- match predicción más cercana a fecha de la lesson contra outcome real
  with matched as (
    select
      l.matter_id,
      l.outcome as actual,
      (
        select cp.primary_outcome from case_predictions cp
         where cp.firm_id = p_firm_id
           and cp.matter_id = l.matter_id
           and cp.generated_at <= l.created_at
         order by cp.generated_at desc limit 1
      ) as predicted,
      (
        select cp.confidence from case_predictions cp
         where cp.firm_id = p_firm_id
           and cp.matter_id = l.matter_id
           and cp.generated_at <= l.created_at
         order by cp.generated_at desc limit 1
      ) as confidence
      from case_lessons l
     where l.firm_id = p_firm_id
       and l.outcome <> 'unknown'
       and l.created_at >= v_window
  ),
  filtered as (select * from matched where predicted is not null and predicted <> 'unknown')
  select
    count(*)::int,
    count(*) filter (where actual = predicted)::int,
    coalesce(avg(confidence) filter (where actual = predicted), 0),
    coalesce(avg(confidence) filter (where actual <> predicted), 0),
    coalesce(jsonb_object_agg(actual, cnt), '{}'::jsonb)
    into v_total, v_correct, v_avg_confidence_correct, v_avg_confidence_wrong, v_by_outcome
    from (
      select actual, predicted, confidence,
             count(*) as cnt
        from filtered
       group by actual, predicted, confidence
    ) g, filtered f;

  -- fallback si no hay rows
  return jsonb_build_object(
    'total_predictions_with_outcome', coalesce(v_total, 0),
    'correct', coalesce(v_correct, 0),
    'accuracy_pct',
      case when coalesce(v_total, 0) = 0 then 0
           else round((v_correct::numeric / v_total) * 100, 1)
      end,
    'avg_confidence_when_correct', coalesce(v_avg_confidence_correct, 0),
    'avg_confidence_when_wrong', coalesce(v_avg_confidence_wrong, 0),
    'sample_size', coalesce(v_total, 0)
  );
exception when others then
  return jsonb_build_object(
    'total_predictions_with_outcome', 0,
    'correct', 0,
    'accuracy_pct', 0,
    'error', SQLERRM
  );
end;
$$;

-- KPIs ejecutivos del firm (hoy)
create or replace function lexai_executive_kpis(p_firm_id uuid)
returns jsonb language sql stable as $$
  select jsonb_build_object(
    'matters_total', (select count(*) from matters where firm_id = p_firm_id),
    'matters_active', (select count(*) from matters where firm_id = p_firm_id and status = 'activo'),
    'matters_closed_30d', (select count(*) from matters
                            where firm_id = p_firm_id and status in ('cerrado','archivado')
                              and updated_at >= now() - interval '30 days'),
    'invoiced_mtd_cop', coalesce((
      select sum(total_cop) from invoices
       where firm_id = p_firm_id and status not in ('draft','void')
         and date_trunc('month', created_at) = date_trunc('month', now())
    ), 0),
    'collected_mtd_cop', coalesce((
      select sum(paid_amount_cop) from invoices
       where firm_id = p_firm_id and paid_at is not null
         and date_trunc('month', paid_at) = date_trunc('month', now())
    ), 0),
    'ar_total_cop', coalesce((
      select sum(total_cop - paid_amount_cop) from invoices
       where firm_id = p_firm_id and status in ('sent','partially_paid','overdue')
    ), 0),
    'ar_overdue_cop', coalesce((
      select sum(total_cop - paid_amount_cop) from invoices
       where firm_id = p_firm_id and status in ('sent','partially_paid','overdue')
         and due_date is not null and due_date < current_date
    ), 0),
    'billable_minutes_30d', coalesce((
      select sum(duration_min) from time_entries
       where firm_id = p_firm_id and billable = true and ended_at is not null
         and ended_at >= now() - interval '30 days'
    ), 0),
    'leads_open', (select count(*) from leads where firm_id = p_firm_id and status = 'open'),
    'tasks_open', (select count(*) from tasks where firm_id = p_firm_id
                     and status in ('open','in_progress','blocked')),
    'tasks_overdue', (select count(*) from tasks where firm_id = p_firm_id
                       and status in ('open','in_progress','blocked')
                       and due_at is not null and due_at < now()),
    'predictions_30d', (select count(*) from case_predictions where firm_id = p_firm_id
                         and generated_at >= now() - interval '30 days')
  );
$$;
