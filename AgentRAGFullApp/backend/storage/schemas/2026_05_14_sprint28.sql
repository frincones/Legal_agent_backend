-- ============================================================
-- LexAI · Sprint 28 · Hardening + Compliance + Launch polish
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Depends on: firms, users, admin_users
-- ============================================================

-- ------------------------------------------------------------
-- 1. arco_requests · solicitudes Habeas Data ARCO
-- ------------------------------------------------------------
create table if not exists arco_requests (
  id                  uuid primary key default gen_random_uuid(),
  firm_id             uuid references firms(id) on delete set null,
  user_id             uuid references users(id) on delete set null,
  request_kind        text not null
    check (request_kind in ('access','rectification','cancellation','opposition','portability','consent_revocation')),
  status              text not null default 'open'
    check (status in ('open','in_progress','requires_info','approved','rejected','completed','cancelled')),
  priority            text not null default 'normal'
    check (priority in ('low','normal','high','urgent')),
  -- Datos del solicitante (puede no ser un user firma si es externo)
  requestor_email     text not null,
  requestor_name      text,
  requestor_doc_id    text,                                  -- cédula/NIT
  -- Detalle de la solicitud
  subject             text not null,
  description         text not null,
  data_subject_id     text,                                  -- titular del dato (si distinto al requestor)
  evidence_url        text,                                  -- URL a documento que respalda
  -- Procesamiento
  assigned_to         uuid references admin_users(id),
  response_text       text,
  response_at         timestamptz,
  -- SLAs · Ley 1581/2012 Art. 14 = 15 días hábiles
  due_at              timestamptz default (now() + interval '15 days'),
  closed_at           timestamptz,
  -- Compliance
  source_law          text default 'Ley 1581/2012 (CO)',
  metadata            jsonb default '{}'::jsonb,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);
create index if not exists arco_requests_status_idx on arco_requests (status, priority desc, due_at);
create index if not exists arco_requests_firm_idx on arco_requests (firm_id) where firm_id is not null;
create index if not exists arco_requests_email_idx on arco_requests (requestor_email);
create index if not exists arco_requests_assigned_idx on arco_requests (assigned_to) where assigned_to is not null;

alter table arco_requests enable row level security;
drop policy if exists arco_requests_select on arco_requests;
drop policy if exists arco_requests_insert on arco_requests;
drop policy if exists arco_requests_modify on arco_requests;
-- Cliente firm puede ver solo las suyas
create policy arco_requests_select on arco_requests for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
-- Cualquiera autenticado puede crear (con su firm)
create policy arco_requests_insert on arco_requests for insert
  with check (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
-- Modify · solo admin SaaS
create policy arco_requests_modify on arco_requests for update
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- Mensajes / threading de la solicitud
create table if not exists arco_request_messages (
  id              uuid primary key default gen_random_uuid(),
  request_id      uuid not null references arco_requests(id) on delete cascade,
  author_kind     text not null check (author_kind in ('requestor','admin','system')),
  admin_user_id   uuid references admin_users(id),
  user_id         uuid references users(id),
  body            text not null,
  internal_note   boolean default false,
  created_at      timestamptz default now()
);
create index if not exists arco_request_messages_req_idx on arco_request_messages (request_id, created_at);

alter table arco_request_messages enable row level security;
drop policy if exists arco_msg_select on arco_request_messages;
drop policy if exists arco_msg_modify on arco_request_messages;
create policy arco_msg_select on arco_request_messages for select
  using (
    exists (select 1 from arco_requests r
             where r.id = request_id
               and (r.firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role'))
    and (not internal_note or is_saas_admin() or auth.role() = 'service_role')
  );
create policy arco_msg_modify on arco_request_messages for all
  using (is_saas_admin() or auth.role() = 'service_role'
         or exists (select 1 from arco_requests r where r.id = request_id and r.firm_id = auth_firm_id()))
  with check (is_saas_admin() or auth.role() = 'service_role'
              or exists (select 1 from arco_requests r where r.id = request_id and r.firm_id = auth_firm_id()));

-- Trigger · updated_at
drop trigger if exists trg_arco_requests_updated_at on arco_requests;
create trigger trg_arco_requests_updated_at
  before update on arco_requests
  for each row execute function tg_set_updated_at();

-- ------------------------------------------------------------
-- 2. status_components · componentes del sistema monitoreados
-- ------------------------------------------------------------
create table if not exists status_components (
  key                text primary key,
  name               text not null,
  description        text,
  sort_order         int default 100,
  active             boolean default true,
  current_status     text default 'operational'
    check (current_status in ('operational','degraded','partial_outage','major_outage','maintenance')),
  current_status_since timestamptz default now(),
  created_at         timestamptz default now()
);

alter table status_components enable row level security;
drop policy if exists status_components_select on status_components;
drop policy if exists status_components_modify on status_components;
create policy status_components_select on status_components for select using (active = true);
create policy status_components_modify on status_components for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. status_checks · historial de probes
-- ------------------------------------------------------------
create table if not exists status_checks (
  id              bigserial primary key,
  component_key   text not null references status_components(key) on delete cascade,
  status          text not null
    check (status in ('operational','degraded','partial_outage','major_outage','maintenance')),
  latency_ms      int,
  error_text      text,
  metadata        jsonb default '{}'::jsonb,
  checked_at      timestamptz default now()
);
create index if not exists status_checks_component_time_idx
  on status_checks (component_key, checked_at desc);

alter table status_checks enable row level security;
drop policy if exists status_checks_select on status_checks;
drop policy if exists status_checks_modify on status_checks;
create policy status_checks_select on status_checks for select using (true);
create policy status_checks_modify on status_checks for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 4. status_incidents · incidents reportados o creados por admin
-- ------------------------------------------------------------
create table if not exists status_incidents (
  id              uuid primary key default gen_random_uuid(),
  title           text not null,
  body            text,
  impact          text not null default 'minor'
    check (impact in ('minor','major','critical','maintenance')),
  status          text not null default 'investigating'
    check (status in ('investigating','identified','monitoring','resolved')),
  components      text[] default '{}',
  started_at      timestamptz default now(),
  resolved_at     timestamptz,
  created_by      uuid references admin_users(id),
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists status_incidents_open_idx
  on status_incidents (started_at desc) where resolved_at is null;
create index if not exists status_incidents_recent_idx
  on status_incidents (started_at desc);

alter table status_incidents enable row level security;
drop policy if exists status_incidents_select on status_incidents;
drop policy if exists status_incidents_modify on status_incidents;
create policy status_incidents_select on status_incidents for select using (true);
create policy status_incidents_modify on status_incidents for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- Updates de un incident (timeline)
create table if not exists status_incident_updates (
  id              uuid primary key default gen_random_uuid(),
  incident_id     uuid not null references status_incidents(id) on delete cascade,
  status          text not null
    check (status in ('investigating','identified','monitoring','resolved')),
  message         text not null,
  created_by      uuid references admin_users(id),
  created_at      timestamptz default now()
);
create index if not exists status_incident_updates_idx
  on status_incident_updates (incident_id, created_at);

alter table status_incident_updates enable row level security;
drop policy if exists status_incident_updates_select on status_incident_updates;
drop policy if exists status_incident_updates_modify on status_incident_updates;
create policy status_incident_updates_select on status_incident_updates for select using (true);
create policy status_incident_updates_modify on status_incident_updates for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- Trigger updated_at
drop trigger if exists trg_status_incidents_updated_at on status_incidents;
create trigger trg_status_incidents_updated_at
  before update on status_incidents
  for each row execute function tg_set_updated_at();

-- ============================================================
-- RPC · status_summary para el endpoint público
-- ============================================================
create or replace function lexai_status_summary()
returns jsonb
language sql
stable
as $$
  with comp as (
    select key, name, description, current_status, current_status_since, sort_order
      from status_components
     where active = true
     order by sort_order
  ),
  recent_incidents as (
    select id, title, body, impact, status, components, started_at, resolved_at
      from status_incidents
     where started_at >= now() - interval '90 days'
     order by started_at desc
     limit 20
  ),
  uptime_30d as (
    select c.key,
      count(*) filter (where sc.status = 'operational')::numeric / nullif(count(*), 0) * 100 as pct
      from status_components c
      left join status_checks sc on sc.component_key = c.key
       and sc.checked_at >= now() - interval '30 days'
     where c.active = true
     group by c.key
  )
  select jsonb_build_object(
    'overall_status', case
      when exists (select 1 from comp where current_status = 'major_outage') then 'major_outage'
      when exists (select 1 from comp where current_status = 'partial_outage') then 'partial_outage'
      when exists (select 1 from comp where current_status = 'degraded') then 'degraded'
      when exists (select 1 from comp where current_status = 'maintenance') then 'maintenance'
      else 'operational' end,
    'components', (
      select jsonb_agg(jsonb_build_object(
        'key', c.key, 'name', c.name, 'description', c.description,
        'status', c.current_status, 'status_since', c.current_status_since,
        'uptime_30d_pct', coalesce((select pct from uptime_30d u where u.key = c.key), 100)
      )) from comp c
    ),
    'recent_incidents', (
      select coalesce(jsonb_agg(to_jsonb(i.*)), '[]'::jsonb) from recent_incidents i
    ),
    'snapshot_at', now()
  );
$$;
grant execute on function lexai_status_summary() to authenticated, service_role, anon;

-- ============================================================
-- SEED · 5 componentes iniciales
-- ============================================================
insert into status_components (key, name, description, sort_order) values
  ('api',      'API Backend',          'FastAPI servidor principal · Railway',                       10),
  ('frontend', 'Frontend',             'Next.js · Vercel CDN',                                       20),
  ('database', 'Base de datos',        'Supabase Postgres + pgvector',                               30),
  ('storage',  'Storage',              'Supabase Storage · documentos',                              40),
  ('ai',       'Modelos IA',           'OpenAI API · GPT-4o · embeddings',                           50),
  ('voice',    'Asistente de voz',     'OpenAI Realtime + WebSocket relay',                          60),
  ('email',    'Email transactional',  'Proveedor configurable (Resend/Postmark/SMTP)',              70)
on conflict (key) do nothing;

-- ============================================================
-- Done · Sprint 28 migration
-- ============================================================
