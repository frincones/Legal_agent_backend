-- ============================================================
-- LexAI · F6 · Agent traces (observabilidad)
-- Migration date: 2026-05-03
-- Idempotent · additive
-- ============================================================

create table if not exists agent_traces (
  id              bigserial primary key,
  run_id          uuid,
  session_id      uuid,
  firm_id         uuid references firms(id) on delete cascade,
  matter_id       uuid,
  kind            text not null check (kind in ('tool_call', 'subagent', 'llm_call', 'ui_command')),
  name            text not null,
  input           jsonb,
  output_preview  jsonb,
  duration_ms     int,
  tokens_in       int,
  tokens_out      int,
  cost_usd        numeric(10,6),
  error           text,
  started_at      timestamptz default now()
);

create index if not exists agent_traces_firm_started_idx
  on agent_traces (firm_id, started_at desc);
create index if not exists agent_traces_matter_idx
  on agent_traces (matter_id, started_at desc) where matter_id is not null;
create index if not exists agent_traces_kind_idx
  on agent_traces (firm_id, kind, started_at desc);
create index if not exists agent_traces_errors_idx
  on agent_traces (firm_id, started_at desc) where error is not null;

alter table agent_traces enable row level security;

drop policy if exists agent_traces_select on agent_traces;
drop policy if exists agent_traces_modify on agent_traces;
create policy agent_traces_select on agent_traces for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy agent_traces_modify on agent_traces for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop trigger if exists trg_agent_traces_firm_id on agent_traces;
create trigger trg_agent_traces_firm_id
  before insert on agent_traces
  for each row execute function set_firm_id_from_jwt();

-- RPC summary para /admin/eval
create or replace function lexai_trace_summary(p_firm_id uuid, p_days int default 7)
returns jsonb
language sql
stable
as $$
  with traces as (
    select * from agent_traces
    where firm_id = p_firm_id
      and started_at >= now() - (p_days || ' days')::interval
  )
  select jsonb_build_object(
    'period_days', p_days,
    'total_traces', (select count(*) from traces),
    'tool_calls', (select count(*) from traces where kind = 'tool_call'),
    'subagent_runs', (select count(*) from traces where kind = 'subagent'),
    'errors', (select count(*) from traces where error is not null),
    'p50_ms', (select percentile_disc(0.5) within group (order by duration_ms) from traces where duration_ms is not null),
    'p95_ms', (select percentile_disc(0.95) within group (order by duration_ms) from traces where duration_ms is not null),
    'cost_usd_total', (select coalesce(sum(cost_usd), 0) from traces),
    'tokens_in_total', (select coalesce(sum(tokens_in), 0) from traces),
    'tokens_out_total', (select coalesce(sum(tokens_out), 0) from traces),
    'top_tools', (
      select coalesce(jsonb_agg(jsonb_build_object('name', name, 'count', n)
        order by n desc), '[]'::jsonb)
      from (
        select name, count(*) as n from traces
        where kind = 'tool_call'
        group by name
        order by n desc limit 10
      ) t
    )
  );
$$;
