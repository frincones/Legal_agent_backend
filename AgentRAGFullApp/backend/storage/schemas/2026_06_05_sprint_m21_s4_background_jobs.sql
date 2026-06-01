-- Sprint M21.S4 · Background Agents · jobs config + run logs.
--
-- Soporta 4 agents:
--   - learn_from_seed_docs     · trigger: nuevo doc en firm_seed_documents status=pending
--   - extract_practice_patterns · cron: nightly
--   - derogation_sweeper        · cron: daily 04:00 UTC
--   - matter_summary_refresher  · trigger: matter_history >= 10 nuevos eventos
--
-- Multi-tenant RLS via current_user_firm_id() (helper de Sprint 1).
-- Idempotente: CREATE IF NOT EXISTS + DO blocks.

-- ─── Tabla 1: firm_background_jobs (config por firm) ─────────
create table if not exists firm_background_jobs (
    job_id          uuid primary key default gen_random_uuid(),
    firm_id         uuid not null,
    agent_name      text not null,            -- 'learn_from_seed_docs' | 'extract_practice_patterns' | ...
    enabled         boolean not null default true,
    schedule_cron   text,                     -- "0 4 * * *" (null = trigger-based)
    last_run_at     timestamptz,
    last_run_status text,                     -- 'ok' | 'error' | 'running' | 'skipped'
    config          jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint uq_firm_agent unique (firm_id, agent_name)
);

create index if not exists ix_bg_jobs_firm on firm_background_jobs(firm_id);
create index if not exists ix_bg_jobs_enabled on firm_background_jobs(enabled) where enabled = true;

-- ─── Tabla 2: agent_run_logs (audit append-only) ─────────────
create table if not exists agent_run_logs (
    run_id          uuid primary key default gen_random_uuid(),
    firm_id         uuid not null,
    agent_name      text not null,
    job_id          uuid,                     -- FK soft a firm_background_jobs
    trigger_kind    text not null,            -- 'cron' | 'event' | 'manual'
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    duration_ms     integer,
    status          text not null,            -- 'running' | 'ok' | 'error' | 'timeout'
    items_processed integer default 0,
    items_succeeded integer default 0,
    items_failed    integer default 0,
    output_summary  text,
    error_message   text,
    cost_usd        numeric(10,4),
    metadata        jsonb not null default '{}'::jsonb
);

create index if not exists ix_run_logs_firm_started on agent_run_logs(firm_id, started_at desc);
create index if not exists ix_run_logs_agent on agent_run_logs(agent_name, started_at desc);

-- ─── RLS multi-tenant ────────────────────────────────────────
alter table firm_background_jobs enable row level security;
alter table agent_run_logs enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where tablename = 'firm_background_jobs' and policyname = 'firm_bg_jobs_rls_select') then
        create policy firm_bg_jobs_rls_select on firm_background_jobs for select
            using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename = 'firm_background_jobs' and policyname = 'firm_bg_jobs_rls_all') then
        create policy firm_bg_jobs_rls_all on firm_background_jobs for all
            using (firm_id = current_user_firm_id())
            with check (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename = 'agent_run_logs' and policyname = 'agent_run_logs_rls_select') then
        create policy agent_run_logs_rls_select on agent_run_logs for select
            using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename = 'agent_run_logs' and policyname = 'agent_run_logs_rls_insert') then
        create policy agent_run_logs_rls_insert on agent_run_logs for insert
            with check (firm_id = current_user_firm_id());
    end if;
end$$;

-- ─── Helper: seed default jobs por firm (idempotente) ───────
create or replace function seed_default_background_jobs(p_firm_id uuid) returns void
language plpgsql security definer as $$
begin
    insert into firm_background_jobs (firm_id, agent_name, enabled, schedule_cron, config)
    values
        (p_firm_id, 'learn_from_seed_docs',     true, null,         '{"trigger":"seed_doc_uploaded"}'::jsonb),
        (p_firm_id, 'extract_practice_patterns', true, '0 3 * * *', '{"trigger":"nightly_03_utc"}'::jsonb),
        (p_firm_id, 'derogation_sweeper',        true, '0 4 * * *', '{"top_n_citations":100}'::jsonb),
        (p_firm_id, 'matter_summary_refresher',  true, null,         '{"min_history_events":10}'::jsonb)
    on conflict (firm_id, agent_name) do nothing;
end;
$$;

-- ─── Validation block ────────────────────────────────────────
do $$
declare
    bg_exists boolean;
    log_exists boolean;
begin
    select exists(select 1 from information_schema.tables where table_schema='public' and table_name='firm_background_jobs')
        into bg_exists;
    select exists(select 1 from information_schema.tables where table_schema='public' and table_name='agent_run_logs')
        into log_exists;
    if not (bg_exists and log_exists) then
        raise exception 'M21.S4 migration validation failed: bg=% logs=%', bg_exists, log_exists;
    end if;
    raise notice 'M21.S4 migration OK: firm_background_jobs + agent_run_logs creadas con RLS';
end$$;
