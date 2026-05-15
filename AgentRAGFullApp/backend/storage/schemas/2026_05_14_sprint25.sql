-- ============================================================
-- LexAI · Sprint 25 · Granular Entitlements (Modules + Quotas + Plans)
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Depends on: Sprint 6 (subscription_plans, firm_subscriptions),
--             Sprint 23 (usage_counters), Sprint 24 (admin_users)
-- Strategy: Aditivo · subscription_plans.f_*/q_* columns se mantienen
--           para compat pero el nuevo sistema es source-of-truth.
-- ============================================================

-- ------------------------------------------------------------
-- 1. modules · catálogo declarativo de módulos
-- ------------------------------------------------------------
create table if not exists modules (
  key                  text primary key,
  name                 text not null,
  description          text,
  category             text not null default 'general'
    check (category in ('core','productivity','ai','docs','calc','collaboration',
                        'automation','analytics','integrations','client_facing',
                        'billing','marketplace','admin_only','experimental')),
  ui_route             text,                            -- ruta principal en frontend (opcional)
  is_core              boolean not null default false,  -- core = no se puede desactivar
  kill_switch_default  boolean not null default false,  -- si no hay row en plan_modules: este valor
  sort_order           int default 100,
  metadata             jsonb default '{}'::jsonb,
  created_at           timestamptz default now(),
  updated_at           timestamptz default now()
);
create index if not exists modules_category_idx on modules (category);
create index if not exists modules_route_idx on modules (ui_route);

alter table modules enable row level security;
drop policy if exists modules_select on modules;
drop policy if exists modules_modify on modules;
create policy modules_select on modules for select using (true);
create policy modules_modify on modules for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 2. quota_types · catálogo declarativo de cuotas
-- ------------------------------------------------------------
create table if not exists quota_types (
  key                  text primary key,
  name                 text not null,
  description          text,
  unit                 text not null default 'count',
  reset_period         text not null default 'monthly'
    check (reset_period in ('monthly','daily','weekly','annual','never')),
  enforcement          text not null default 'hard'
    check (enforcement in ('hard','soft','tracking_only')),
  sort_order           int default 100,
  created_at           timestamptz default now(),
  updated_at           timestamptz default now()
);
create index if not exists quota_types_period_idx on quota_types (reset_period);

alter table quota_types enable row level security;
drop policy if exists quota_types_select on quota_types;
drop policy if exists quota_types_modify on quota_types;
create policy quota_types_select on quota_types for select using (true);
create policy quota_types_modify on quota_types for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. plan_modules · qué módulos incluye cada plan
-- ------------------------------------------------------------
create table if not exists plan_modules (
  plan_code            text not null references subscription_plans(code) on delete cascade,
  module_key           text not null references modules(key) on delete cascade,
  enabled              boolean not null default false,
  updated_at           timestamptz default now(),
  primary key (plan_code, module_key)
);
create index if not exists plan_modules_module_idx on plan_modules (module_key, enabled);

alter table plan_modules enable row level security;
drop policy if exists plan_modules_select on plan_modules;
drop policy if exists plan_modules_modify on plan_modules;
create policy plan_modules_select on plan_modules for select using (true);
create policy plan_modules_modify on plan_modules for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 4. plan_quotas · límites de cuotas por plan
-- ------------------------------------------------------------
create table if not exists plan_quotas (
  plan_code            text not null references subscription_plans(code) on delete cascade,
  quota_type_key       text not null references quota_types(key) on delete cascade,
  limit_value          bigint,                          -- null = unlimited
  soft_cap_pct         int default 80 check (soft_cap_pct between 0 and 100),
  updated_at           timestamptz default now(),
  primary key (plan_code, quota_type_key)
);

alter table plan_quotas enable row level security;
drop policy if exists plan_quotas_select on plan_quotas;
drop policy if exists plan_quotas_modify on plan_quotas;
create policy plan_quotas_select on plan_quotas for select using (true);
create policy plan_quotas_modify on plan_quotas for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 5. firm_module_overrides · override per-firm (custom contracts)
-- ------------------------------------------------------------
create table if not exists firm_module_overrides (
  firm_id              uuid not null references firms(id) on delete cascade,
  module_key           text not null references modules(key) on delete cascade,
  enabled              boolean not null,
  reason               text,
  expires_at           timestamptz,
  created_by           uuid references admin_users(id),
  created_at           timestamptz default now(),
  primary key (firm_id, module_key)
);
create index if not exists firm_module_overrides_firm_idx on firm_module_overrides (firm_id);

alter table firm_module_overrides enable row level security;
drop policy if exists firm_module_overrides_select on firm_module_overrides;
drop policy if exists firm_module_overrides_modify on firm_module_overrides;
create policy firm_module_overrides_select on firm_module_overrides for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy firm_module_overrides_modify on firm_module_overrides for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 6. firm_quota_overrides · override de cuotas per-firm
-- ------------------------------------------------------------
create table if not exists firm_quota_overrides (
  firm_id              uuid not null references firms(id) on delete cascade,
  quota_type_key       text not null references quota_types(key) on delete cascade,
  limit_value          bigint,                          -- null = unlimited
  reason               text,
  expires_at           timestamptz,
  created_by           uuid references admin_users(id),
  created_at           timestamptz default now(),
  primary key (firm_id, quota_type_key)
);
create index if not exists firm_quota_overrides_firm_idx on firm_quota_overrides (firm_id);

alter table firm_quota_overrides enable row level security;
drop policy if exists firm_quota_overrides_select on firm_quota_overrides;
drop policy if exists firm_quota_overrides_modify on firm_quota_overrides;
create policy firm_quota_overrides_select on firm_quota_overrides for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy firm_quota_overrides_modify on firm_quota_overrides for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ============================================================
-- RPCs · resolución determinística
-- ============================================================

-- has_module: override (no expirado) > plan_modules > kill_switch_default
create or replace function lexai_has_module(p_firm_id uuid, p_module_key text)
returns boolean
language sql
stable
as $$
  with module_meta as (
    select is_core, kill_switch_default from modules where key = p_module_key
  ),
  override as (
    select enabled from firm_module_overrides
     where firm_id = p_firm_id and module_key = p_module_key
       and (expires_at is null or expires_at > now())
     limit 1
  ),
  plan_value as (
    select pm.enabled
      from plan_modules pm
      join firm_subscriptions s on s.plan_code = pm.plan_code
     where s.firm_id = p_firm_id and pm.module_key = p_module_key
     limit 1
  )
  select coalesce(
    (select is_core from module_meta),                           -- core siempre TRUE
    (select enabled from override),
    (select enabled from plan_value),
    (select kill_switch_default from module_meta),
    false
  );
$$;
grant execute on function lexai_has_module(uuid, text) to authenticated, service_role;

-- quota_for: override > plan_quotas > unlimited (null) > 0
create or replace function lexai_quota_for(p_firm_id uuid, p_quota_key text)
returns jsonb
language sql
stable
as $$
  with quota_meta as (
    select reset_period, enforcement, name, unit from quota_types where key = p_quota_key
  ),
  override as (
    select limit_value from firm_quota_overrides
     where firm_id = p_firm_id and quota_type_key = p_quota_key
       and (expires_at is null or expires_at > now())
     limit 1
  ),
  plan_value as (
    select pq.limit_value, pq.soft_cap_pct
      from plan_quotas pq
      join firm_subscriptions s on s.plan_code = pq.plan_code
     where s.firm_id = p_firm_id and pq.quota_type_key = p_quota_key
     limit 1
  ),
  period_start as (
    select case
      when (select reset_period from quota_meta) = 'monthly' then date_trunc('month', now())::date
      when (select reset_period from quota_meta) = 'daily' then current_date
      when (select reset_period from quota_meta) = 'weekly' then date_trunc('week', now())::date
      when (select reset_period from quota_meta) = 'annual' then date_trunc('year', now())::date
      else date '2000-01-01'                                     -- never
    end as ps
  ),
  used as (
    select coalesce(count, 0) as used
      from usage_counters
     where firm_id = p_firm_id
       and kind = case p_quota_key
                    when 'llm_calls'        then 'llm_call'
                    when 'voice_minutes'    then 'voice_minute'
                    when 'documents_uploaded' then 'document_upload'
                    when 'email_accounts'   then 'email_sync'
                    when 'judicial_subscriptions' then 'judicial_poll'
                    when 'wizards_generated' then 'wizard_generated'
                    else p_quota_key
                  end
       and period_start = (select ps from period_start)
     limit 1
  )
  select jsonb_build_object(
    'quota_key', p_quota_key,
    'name', (select name from quota_meta),
    'unit', (select unit from quota_meta),
    'limit',
      coalesce(
        (select limit_value from override),
        (select limit_value from plan_value),
        0
      ),
    'soft_cap_pct', coalesce((select soft_cap_pct from plan_value), 80),
    'used', coalesce((select used from used), 0),
    'remaining',
      case
        when coalesce((select limit_value from override), (select limit_value from plan_value)) is null then null
        else greatest(0, coalesce((select limit_value from override), (select limit_value from plan_value), 0) - coalesce((select used from used), 0))
      end,
    'period_start', (select ps from period_start),
    'reset_period', (select reset_period from quota_meta),
    'enforcement', (select enforcement from quota_meta),
    'has_override', exists (select 1 from override)
  );
$$;
grant execute on function lexai_quota_for(uuid, text) to authenticated, service_role;

-- entitlements snapshot · cachéable en frontend
create or replace function lexai_entitlements(p_firm_id uuid default null)
returns jsonb
language sql
stable
as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
  plan_info as (
    select s.plan_code, s.status, s.trial_ends_at, s.current_period_end,
           p.name as plan_name
      from firm_subscriptions s
      join subscription_plans p on p.code = s.plan_code
     where s.firm_id = (select id from f)
     limit 1
  ),
  modules_resolved as (
    select m.key,
           lexai_has_module((select id from f), m.key) as enabled,
           m.category, m.name, m.is_core, m.ui_route,
           exists (
             select 1 from firm_module_overrides fmo
              where fmo.firm_id = (select id from f) and fmo.module_key = m.key
                and (fmo.expires_at is null or fmo.expires_at > now())
           ) as has_override
      from modules m
  ),
  quotas_resolved as (
    select q.key, lexai_quota_for((select id from f), q.key) as data
      from quota_types q
  )
  select jsonb_build_object(
    'firm_id', (select id from f),
    'plan', coalesce(
      (select jsonb_build_object('code', plan_code, 'name', plan_name, 'status', status,
                                 'trial_ends_at', trial_ends_at,
                                 'current_period_end', current_period_end)
         from plan_info),
      jsonb_build_object('code', 'free', 'name', 'Free Trial', 'status', 'trialing')
    ),
    'modules', (
      select jsonb_object_agg(key, jsonb_build_object(
        'enabled', enabled, 'category', category, 'name', name,
        'is_core', is_core, 'has_override', has_override, 'ui_route', ui_route
      )) from modules_resolved
    ),
    'quotas', (select jsonb_object_agg(key, data) from quotas_resolved),
    'snapshot_at', now()
  );
$$;
grant execute on function lexai_entitlements(uuid) to authenticated, service_role;

-- ============================================================
-- SEED · catálogo de módulos (70 entradas) y quota_types (8 entradas)
-- ============================================================

insert into quota_types (key, name, unit, reset_period, enforcement, sort_order) values
  ('llm_calls', 'Llamadas IA', 'calls', 'monthly', 'hard', 10),
  ('voice_minutes', 'Minutos de voz', 'minutes', 'monthly', 'hard', 20),
  ('documents_uploaded', 'Documentos subidos', 'docs', 'monthly', 'hard', 30),
  ('matters_active', 'Casos activos', 'matters', 'never', 'soft', 40),
  ('users', 'Usuarios', 'users', 'never', 'hard', 50),
  ('email_accounts', 'Cuentas de email', 'accounts', 'never', 'hard', 60),
  ('judicial_subscriptions', 'Suscripciones judiciales', 'subscriptions', 'never', 'hard', 70),
  ('wizards_generated', 'Wizards generados', 'wizards', 'monthly', 'soft', 80)
on conflict (key) do nothing;

-- Modules seed (~70 modules · grouped by category)
insert into modules (key, name, description, category, ui_route, is_core, kill_switch_default, sort_order) values
  -- CORE (siempre ON · no se gatean en realidad pero los registramos)
  ('auth', 'Autenticación', 'Login + JWT + sesiones', 'core', null, true, true, 1),
  ('matters', 'Casos', 'Gestión de casos', 'core', '/casos', true, true, 2),
  ('clients', 'Clientes', 'Directorio de clientes', 'core', '/clientes', true, true, 3),
  ('documents', 'Documentos', 'Biblioteca de documentos del caso', 'core', '/documentos', true, true, 4),
  ('search_basic', 'Búsqueda básica', 'Búsqueda full-text simple', 'core', '/buscar', true, true, 5),
  ('profile', 'Perfil', 'Configuración personal', 'core', '/settings/perfil', true, true, 6),
  ('firm_users', 'Usuarios firm', 'Gestión usuarios del despacho', 'core', '/settings/usuarios', true, true, 7),
  ('firm_teams', 'Equipos', 'Sub-equipos del despacho', 'core', '/settings/equipo', true, true, 8),
  ('inicio_dashboard', 'Inicio', 'Dashboard de inicio', 'core', '/inicio', true, true, 9),

  -- PRODUCTIVITY
  ('tasks', 'Tareas', 'To-do asignables', 'productivity', '/tareas', false, false, 100),
  ('my_day', 'Mi día', 'Agenda diaria IA', 'productivity', '/mi-dia', false, false, 101),
  ('calendar', 'Calendario', 'Plazos procesales + agenda', 'productivity', '/calendario', false, false, 102),
  ('notifications', 'Notificaciones', 'Inbox unificado', 'productivity', '/notificaciones', false, false, 103),
  ('saved_filters', 'Filtros guardados', 'Vistas guardadas', 'productivity', null, false, false, 104),
  ('automation_rules', 'Automatizaciones', 'Workflow automation', 'automation', '/automation', false, false, 105),

  -- AI & DOCS (premium)
  ('canvas', 'Live Canvas', 'Editor de escritos con IA streaming', 'ai', '/casos/[id]/canvas', false, false, 200),
  ('canvas_transform', 'Canvas transformaciones', 'Mejorar/formalizar texto', 'ai', null, false, false, 201),
  ('citations_research', 'Research jurisprudencia', 'Búsqueda jurisprudencia con anti-hallucination', 'ai', null, false, false, 202),
  ('citations_validate', 'Validar citas', 'Verificar vigencia normas', 'ai', null, false, false, 203),
  ('doc_qa', 'Q&A documentos', 'Chat con docs del caso', 'ai', null, false, false, 204),
  ('contract_analyzer', 'Análisis de contratos', 'Cláusulas + riesgos + recomendaciones', 'ai', null, false, false, 205),
  ('doc_compare', 'Comparador docs', 'Diff semántico de documentos', 'ai', null, false, false, 206),
  ('doc_analysis', 'Análisis documental', 'Extracción NER', 'ai', null, false, false, 207),
  ('predictions', 'Predicciones', 'Outcome forecasting', 'ai', null, false, false, 208),
  ('judges', 'Jueces', 'Base de jueces + perfiles', 'ai', '/jueces', false, false, 209),
  ('judge_simulator', 'Simulador juez', 'Simulación recepción IA', 'ai', null, false, false, 210),
  ('evidence_checker', 'Verificador evidencia', 'Identidad + inconsistencias + scoring', 'ai', null, false, false, 211),
  ('lessons', 'Lecciones del despacho', 'Memoria + buenas prácticas', 'ai', '/casos/[id]?tab=lecciones', false, false, 212),
  ('daily_briefing', 'Briefing diario', 'Resumen IA del día', 'ai', null, false, false, 213),
  ('ai_insights', 'Insights IA', 'Sugerencias proactivas', 'ai', '/insights', false, false, 214),
  ('voice_agent', 'Asistente de voz', 'Voice agent (Realtime)', 'ai', null, false, false, 215),
  ('ai_chat', 'Chat IA', 'Conversación HTTP', 'ai', null, false, false, 216),
  ('template_intelligence', 'Auto-fill plantillas', 'LLM extrae variables', 'ai', null, false, false, 217),

  -- CALC (calculadoras)
  ('calc_liquidacion', 'Calc liquidación laboral', 'CST + Ley 50/1990', 'calc', '/liquidacion', false, false, 300),
  ('calc_prescripcion', 'Calc prescripción', 'CC, C.Co., CST, CGP, CPP', 'calc', '/calc/prescripcion', false, false, 301),
  ('calc_intereses', 'Calc intereses moratorios', 'Decreto 519/2007', 'calc', '/calc/intereses', false, false, 302),
  ('calc_plazos', 'Calc plazos procesales', 'CGP, CST, tutela', 'calc', '/calc/plazos', false, false, 303),
  ('calc_pension', 'Calc pensión', 'Ley 100/93', 'calc', '/calc/pension', false, false, 304),

  -- COLLABORATION
  ('comments', 'Comentarios', 'Comments en matters y docs', 'collaboration', null, false, false, 400),
  ('mentions', 'Menciones', 'Inbox @user', 'collaboration', '/menciones', false, false, 401),
  ('presence', 'Presencia', 'Quién está viendo qué', 'collaboration', null, false, false, 402),
  ('activity_feed', 'Feed actividad', 'Timeline del despacho', 'collaboration', '/actividad', false, false, 403),
  ('knowledge_base', 'Knowledge Base', 'KB del despacho con búsqueda semántica', 'collaboration', '/kb', false, false, 404),

  -- INTEGRATIONS
  ('email_ingest', 'Email ingest', 'Gmail/Outlook/IMAP', 'integrations', null, false, false, 500),
  ('calendar_sync', 'Sincronización calendario', 'Google Calendar/Outlook', 'integrations', null, false, false, 501),
  ('whatsapp_integration', 'WhatsApp Business', 'Enviar/recibir mensajes', 'integrations', null, false, false, 502),
  ('judicial_polling', 'Court Watcher', 'Polling judicial automático', 'integrations', null, false, false, 503),
  ('judicial_lookup', 'Búsqueda judicial', 'Lookup directo expedientes', 'integrations', null, false, false, 504),
  ('signatures', 'Firmas electrónicas', 'DocuSign/Certicámara', 'integrations', '/firmas', false, false, 505),
  ('api_public', 'API pública', 'API keys + webhooks', 'integrations', null, false, false, 506),
  ('webhooks_outbound', 'Webhooks salientes', 'Notificar a sistemas externos', 'integrations', null, false, false, 507),
  ('marketplace', 'Marketplace', 'Plantillas oficiales + comunidad', 'marketplace', '/marketplace', false, false, 508),

  -- ANALYTICS
  ('analytics_firm', 'Analytics básico', 'KPIs firma', 'analytics', '/reportes', false, false, 600),
  ('analytics_executive', 'Dashboard ejecutivo', 'Revenue + pipeline + performance', 'analytics', '/dashboard', false, false, 601),
  ('reports_custom', 'Reportes custom', 'Reportes guardados exportables', 'analytics', null, false, false, 602),

  -- CLIENT-FACING
  ('client_portal', 'Portal cliente', 'Magic link + dashboard cliente', 'client_facing', null, false, false, 700),
  ('wizards_public', 'Wizards públicos', 'Trámites ciudadanos', 'client_facing', null, false, false, 701),
  ('intake_forms', 'Formularios intake', 'Captación de leads', 'client_facing', '/intake-forms', false, false, 702),
  ('leads_crm', 'Leads CRM', 'Pipeline prospectos', 'client_facing', '/leads', false, false, 703),

  -- BILLING (financiero, no SaaS)
  ('invoices', 'Facturación', 'Facturas con IVA Colombia', 'billing', '/facturacion', false, true, 800),
  ('time_entries', 'Horas billables', 'Timer + manual', 'billing', null, false, true, 801),
  ('expenses', 'Gastos reembolsables', 'Gastos del caso', 'billing', null, false, true, 802),
  ('trust_accounts', 'Cuentas fiduciarias', 'Fondos cliente + reconciliación', 'billing', '/trust', false, false, 803),

  -- IMPORTS & SYNC
  ('csv_imports', 'Imports CSV', 'Validar + importar masivo', 'productivity', null, false, false, 900),
  ('offline_sync', 'Sync offline', 'Cola de cambios offline', 'productivity', null, false, true, 901),

  -- ADMIN ONLY (no se gatean por plan · siempre disponibles a quien tenga el rol)
  ('hitl', 'HITL gates', 'Human-in-the-loop', 'admin_only', null, false, true, 1000),
  ('audit_logs', 'Audit logs', 'Compliance LFPDPPP', 'admin_only', null, false, true, 1001),
  ('push_notifications', 'Push notifications', 'Web push', 'admin_only', null, false, true, 1002),
  ('sla_reminders', 'SLA reminders', 'Alertas SLA', 'admin_only', null, false, true, 1003)
on conflict (key) do nothing;

-- ------------------------------------------------------------
-- Plan Starter (nuevo) · entre Free y Pro
-- ------------------------------------------------------------
insert into subscription_plans
  (code, name, monthly_cop, annual_cop,
   q_users, q_matters, q_documents_mo, q_llm_calls_mo, q_voice_min_mo,
   q_email_accounts, q_judicial_subs,
   f_email_ingest, f_priority_support)
values
  ('starter', 'Starter', 49000, 490000,
   1, 10, 100, 1000, 180, 0, 10, false, false)
on conflict (code) do nothing;

-- ============================================================
-- SEED · plan_modules (qué módulos incluye cada plan)
-- Estrategia conservadora:
--   - free: solo core + 3 calculadoras + tasks
--   - starter: free + canvas básico + analytics básico + más calc
--   - pro: starter + AI avanzado + integrations + collaboration
--   - firm: pro + analytics ejecutivo + marketplace + KB + signatures
--   - enterprise: TODO
-- ============================================================

-- TODOS los planes incluyen módulos core (is_core=true implica enabled siempre)
insert into plan_modules (plan_code, module_key, enabled)
select p.code, m.key, true
  from subscription_plans p, modules m
 where m.is_core = true
on conflict (plan_code, module_key) do nothing;

-- FREE plan · módulos no-core
insert into plan_modules (plan_code, module_key, enabled) values
  ('free', 'tasks', true),
  ('free', 'my_day', true),
  ('free', 'calendar', true),
  ('free', 'notifications', true),
  ('free', 'calc_liquidacion', true),
  ('free', 'calc_prescripcion', true),
  ('free', 'calc_intereses', true),
  ('free', 'calc_plazos', true),
  ('free', 'calc_pension', true),
  ('free', 'voice_agent', true),
  ('free', 'ai_chat', true),
  ('free', 'invoices', true),
  ('free', 'time_entries', true),
  ('free', 'expenses', true),
  ('free', 'offline_sync', true),
  ('free', 'saved_filters', true),
  ('free', 'daily_briefing', true)
on conflict (plan_code, module_key) do nothing;

-- STARTER plan · free + más
insert into plan_modules (plan_code, module_key, enabled)
select 'starter', module_key, true from plan_modules where plan_code = 'free'
on conflict (plan_code, module_key) do nothing;
insert into plan_modules (plan_code, module_key, enabled) values
  ('starter', 'canvas', true),
  ('starter', 'canvas_transform', true),
  ('starter', 'citations_research', true),
  ('starter', 'citations_validate', true),
  ('starter', 'doc_qa', true),
  ('starter', 'doc_analysis', true),
  ('starter', 'comments', true),
  ('starter', 'mentions', true),
  ('starter', 'activity_feed', true),
  ('starter', 'judicial_lookup', true),
  ('starter', 'analytics_firm', true),
  ('starter', 'template_intelligence', true),
  ('starter', 'csv_imports', true),
  ('starter', 'ai_insights', true)
on conflict (plan_code, module_key) do nothing;

-- PRO plan · starter + más
insert into plan_modules (plan_code, module_key, enabled)
select 'pro', module_key, true from plan_modules where plan_code = 'starter'
on conflict (plan_code, module_key) do nothing;
insert into plan_modules (plan_code, module_key, enabled) values
  ('pro', 'contract_analyzer', true),
  ('pro', 'doc_compare', true),
  ('pro', 'predictions', true),
  ('pro', 'judges', true),
  ('pro', 'judge_simulator', true),
  ('pro', 'evidence_checker', true),
  ('pro', 'lessons', true),
  ('pro', 'presence', true),
  ('pro', 'email_ingest', true),
  ('pro', 'calendar_sync', true),
  ('pro', 'judicial_polling', true),
  ('pro', 'whatsapp_integration', true),
  ('pro', 'signatures', true),
  ('pro', 'automation_rules', true),
  ('pro', 'intake_forms', true),
  ('pro', 'leads_crm', true),
  ('pro', 'trust_accounts', true),
  ('pro', 'client_portal', true),
  ('pro', 'wizards_public', true)
on conflict (plan_code, module_key) do nothing;

-- FIRM plan · pro + más
insert into plan_modules (plan_code, module_key, enabled)
select 'firm', module_key, true from plan_modules where plan_code = 'pro'
on conflict (plan_code, module_key) do nothing;
insert into plan_modules (plan_code, module_key, enabled) values
  ('firm', 'analytics_executive', true),
  ('firm', 'reports_custom', true),
  ('firm', 'knowledge_base', true),
  ('firm', 'marketplace', true),
  ('firm', 'api_public', true),
  ('firm', 'webhooks_outbound', true)
on conflict (plan_code, module_key) do nothing;

-- ENTERPRISE plan · TODO ON (incluye los no-core restantes)
insert into plan_modules (plan_code, module_key, enabled)
select 'enterprise', key, true from modules where category != 'admin_only'
on conflict (plan_code, module_key) do nothing;

-- ============================================================
-- SEED · plan_quotas
-- ============================================================
insert into plan_quotas (plan_code, quota_type_key, limit_value, soft_cap_pct) values
  -- free
  ('free', 'llm_calls', 200, 80),
  ('free', 'voice_minutes', 60, 80),
  ('free', 'documents_uploaded', 20, 80),
  ('free', 'matters_active', 3, 80),
  ('free', 'users', 1, 100),
  ('free', 'email_accounts', 0, 100),
  ('free', 'judicial_subscriptions', 3, 100),
  ('free', 'wizards_generated', 5, 80),
  -- starter
  ('starter', 'llm_calls', 1000, 80),
  ('starter', 'voice_minutes', 180, 80),
  ('starter', 'documents_uploaded', 100, 80),
  ('starter', 'matters_active', 10, 80),
  ('starter', 'users', 1, 100),
  ('starter', 'email_accounts', 0, 100),
  ('starter', 'judicial_subscriptions', 10, 100),
  ('starter', 'wizards_generated', 20, 80),
  -- pro
  ('pro', 'llm_calls', 5000, 80),
  ('pro', 'voice_minutes', 600, 80),
  ('pro', 'documents_uploaded', 500, 80),
  ('pro', 'matters_active', 30, 80),
  ('pro', 'users', 1, 100),
  ('pro', 'email_accounts', 1, 100),
  ('pro', 'judicial_subscriptions', 30, 100),
  ('pro', 'wizards_generated', 100, 80),
  -- firm
  ('firm', 'llm_calls', 20000, 80),
  ('firm', 'voice_minutes', 2400, 80),
  ('firm', 'documents_uploaded', 2000, 80),
  ('firm', 'matters_active', 200, 80),
  ('firm', 'users', 5, 100),
  ('firm', 'email_accounts', 5, 100),
  ('firm', 'judicial_subscriptions', 200, 100),
  ('firm', 'wizards_generated', 500, 80),
  -- enterprise (null = unlimited)
  ('enterprise', 'llm_calls', null, 80),
  ('enterprise', 'voice_minutes', null, 80),
  ('enterprise', 'documents_uploaded', null, 80),
  ('enterprise', 'matters_active', null, 80),
  ('enterprise', 'users', null, 100),
  ('enterprise', 'email_accounts', null, 100),
  ('enterprise', 'judicial_subscriptions', null, 100),
  ('enterprise', 'wizards_generated', null, 80)
on conflict (plan_code, quota_type_key) do nothing;

-- ============================================================
-- Vista compat (read-only) · simula subscription_plans expandida
-- ============================================================
create or replace view firm_entitlements_resolved as
  select s.firm_id, s.plan_code, lexai_entitlements(s.firm_id) as entitlements
    from firm_subscriptions s;
grant select on firm_entitlements_resolved to authenticated, service_role;

-- ============================================================
-- Done · Sprint 25 migration
-- ============================================================
