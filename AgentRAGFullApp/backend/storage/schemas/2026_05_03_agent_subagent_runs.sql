-- ============================================================
-- LexAI · F3 · Sub-agent runs persistence
-- Migration date: 2026-05-03
-- Idempotent · additive
-- ============================================================

create table if not exists agent_subagent_runs (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid references firms(id) on delete cascade,
  parent_run_id   uuid,
  subagent_name   text not null,
  task            text not null,
  result_jsonb    jsonb,
  tool_calls      jsonb,
  tokens_in       int default 0,
  tokens_out      int default 0,
  duration_ms     int,
  started_at      timestamptz default now()
);

create index if not exists agent_subagent_runs_firm_idx
  on agent_subagent_runs (firm_id, started_at desc);
create index if not exists agent_subagent_runs_subagent_idx
  on agent_subagent_runs (subagent_name, started_at desc);

alter table agent_subagent_runs enable row level security;

drop policy if exists agent_subagent_runs_select on agent_subagent_runs;
drop policy if exists agent_subagent_runs_modify on agent_subagent_runs;
create policy agent_subagent_runs_select on agent_subagent_runs for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy agent_subagent_runs_modify on agent_subagent_runs for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop trigger if exists trg_agent_subagent_runs_firm_id on agent_subagent_runs;
create trigger trg_agent_subagent_runs_firm_id
  before insert on agent_subagent_runs
  for each row execute function set_firm_id_from_jwt();
