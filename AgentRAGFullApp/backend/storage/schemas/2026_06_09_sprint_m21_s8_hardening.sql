-- Sprint M21.S8 · Hardening
--
-- Tablas:
--   - firm_usage_meters     · contadores por firm + resource_type + period (mes)
--   - rate_limit_buckets    · sliding window counters (sin Redis, in-DB)
--   - habeas_data_exports   · trazabilidad de exports Ley 1581/2012
--   - admin_audit_events    · audit centralizado eventos administrativos
--
-- Multi-tenant via current_user_firm_id().

create table if not exists firm_usage_meters (
    firm_id         uuid not null,
    period_month    text not null,                   -- 'YYYY-MM'
    resource_type   text not null,                   -- 'agent_run'|'mcp_call'|'cookbook_run'|'doc_generated'|'llm_tokens'
    count           bigint not null default 0,
    cost_usd        numeric(12,4) not null default 0,
    last_updated    timestamptz not null default now(),
    primary key (firm_id, period_month, resource_type)
);

create table if not exists rate_limit_buckets (
    bucket_id       text primary key,                -- '<firm_id>:<resource>:<window_start_minute>'
    firm_id         uuid not null,
    resource_type   text not null,
    window_start    timestamptz not null,
    count           integer not null default 0,
    last_request_at timestamptz not null default now()
);
create index if not exists ix_rate_limit_buckets_firm_res on rate_limit_buckets(firm_id, resource_type, window_start desc);

-- Auto-cleanup buckets > 24h via cron diario (manual o pg_cron en hardening posterior)
create or replace function cleanup_rate_limit_buckets() returns void
language sql security definer as $$
    delete from rate_limit_buckets where window_start < now() - interval '24 hours';
$$;

create table if not exists habeas_data_exports (
    export_id       uuid primary key default gen_random_uuid(),
    firm_id         uuid not null,
    subject_id      text not null,                   -- cedula | email | client_id
    subject_kind    text not null,                   -- 'cedula'|'email'|'client_id'
    requested_by_user_id uuid,
    requested_at    timestamptz not null default now(),
    completed_at    timestamptz,
    status          text not null default 'pending', -- 'pending'|'processing'|'ready'|'failed'|'expired'
    file_size_bytes bigint,
    download_url    text,                            -- URL temporal (caducidad 7 dias)
    expires_at      timestamptz,
    metadata        jsonb not null default '{}'::jsonb,
    tables_included jsonb not null default '[]'::jsonb,
    error_message   text
);
create index if not exists ix_habeas_exports_firm on habeas_data_exports(firm_id, requested_at desc);
create index if not exists ix_habeas_exports_status on habeas_data_exports(status) where status in ('pending','processing');

create table if not exists admin_audit_events (
    event_id        bigserial primary key,
    firm_id         uuid,
    event_kind      text not null,                   -- 'rate_limit_block'|'habeas_data_request'|'plugin_install'|...
    actor_user_id   uuid,
    actor_role      text,
    ip_address      inet,
    user_agent      text,
    target_resource text,
    summary         text,
    details         jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);
create index if not exists ix_admin_audit_firm_time on admin_audit_events(firm_id, created_at desc) where firm_id is not null;
create index if not exists ix_admin_audit_kind on admin_audit_events(event_kind, created_at desc);

-- RLS
alter table firm_usage_meters enable row level security;
alter table habeas_data_exports enable row level security;
alter table admin_audit_events enable row level security;

do $$
begin
    if not exists (select 1 from pg_policies where tablename='firm_usage_meters' and policyname='firm_usage_meters_rls') then
        create policy firm_usage_meters_rls on firm_usage_meters for select using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='habeas_data_exports' and policyname='habeas_exports_rls_select') then
        create policy habeas_exports_rls_select on habeas_data_exports for select using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='habeas_data_exports' and policyname='habeas_exports_rls_all') then
        create policy habeas_exports_rls_all on habeas_data_exports for all
            using (firm_id = current_user_firm_id())
            with check (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='admin_audit_events' and policyname='admin_audit_rls') then
        create policy admin_audit_rls on admin_audit_events for select using (firm_id is null or firm_id = current_user_firm_id());
    end if;
end$$;

-- Validation
do $$
begin
    if not exists (select 1 from information_schema.tables where table_name='firm_usage_meters') then
        raise exception 'M21.S8 missing firm_usage_meters';
    end if;
    raise notice 'M21.S8 OK: hardening tables creadas';
end$$;
