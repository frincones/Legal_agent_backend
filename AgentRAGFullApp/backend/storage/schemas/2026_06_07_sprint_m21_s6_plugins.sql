-- Sprint M21.S6 · Plugin Marketplace
--
-- 12 plugins builtin (modulos visuales/funcionales que el firm habilita).
-- Mucha funcionalidad YA existe en LexAI (intake forms, billing, dashboards);
-- este sprint la formaliza como "plugin catalog" instalable per-firm.
--
-- Multi-tenant via firm_plugin_installations.

create table if not exists plugin_registry (
    plugin_id       text primary key,                 -- 'intake-forms', 'time-tracker'
    name            text not null,
    category        text not null,                    -- intake|productivity|billing|analytics|compliance|integration|ai
    short_description text not null,
    long_description_md text,
    icon            text,                             -- emoji o nombre de icon lucide
    route_path      text,                             -- ruta UI principal (ej '/inbox/intake-forms')
    api_namespaces  jsonb not null default '[]'::jsonb,  -- ['/v1/intake-forms', ...]
    requires_modules jsonb not null default '[]'::jsonb, -- modulos sprint 25 que requiere
    pricing_tier    text not null default 'free',     -- free|pro|enterprise
    builtin         boolean not null default true,
    version         text not null default '1.0.0',
    documentation_url text,
    screenshots     jsonb default '[]'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists firm_plugin_installations (
    firm_id         uuid not null,
    plugin_id       text not null references plugin_registry(plugin_id) on delete cascade,
    installed_at    timestamptz not null default now(),
    enabled         boolean not null default true,
    installed_by_user_id uuid,
    config          jsonb not null default '{}'::jsonb,
    primary key (firm_id, plugin_id)
);

alter table firm_plugin_installations enable row level security;
do $$
begin
    if not exists (select 1 from pg_policies where tablename='firm_plugin_installations' and policyname='firm_plugin_rls_select') then
        create policy firm_plugin_rls_select on firm_plugin_installations for select
            using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='firm_plugin_installations' and policyname='firm_plugin_rls_all') then
        create policy firm_plugin_rls_all on firm_plugin_installations for all
            using (firm_id = current_user_firm_id())
            with check (firm_id = current_user_firm_id());
    end if;
end$$;

-- ─── Seed: 12 plugins builtin ────────────────────────────────
insert into plugin_registry
    (plugin_id, name, category, short_description, long_description_md, icon, route_path, api_namespaces, requires_modules, pricing_tier)
values
    ('intake-forms',       'Intake Forms',              'intake',      'Formularios publicos para captar leads.',
        'Crea formularios publicos compartibles con clientes potenciales. Captura conflict-check + datos basicos antes de la primera reunion.',
        'clipboard-list', '/intake-forms', '["/v1/intake-forms","/v1/intake-public"]'::jsonb, '[]'::jsonb, 'free'),
    ('matters-workspace',  'Matters Workspace v2',      'productivity','Workspaces de casos con teoria + historial.',
        'Sprint M21. Workspaces individuales por caso (matters_workspace) con teoria del caso, timeline append-only y resumen auto.',
        'folder-kanban', '/v2/matters', '["/v2/matters"]'::jsonb, '[]'::jsonb, 'free'),
    ('time-tracker',       'Time Tracker',              'productivity','Cronometro + entries por matter.',
        'Cronometro start/stop por matter, agrupa por dia/semana, exporta a invoice.',
        'timer', '/time-tracker', '["/v1/time-entries"]'::jsonb, '[]'::jsonb, 'free'),
    ('billing',            'Billing & Invoices',        'billing',     'Facturacion electronica + suscripciones.',
        'Genera facturas DIAN-compliant desde time entries + expenses. Integracion con pasarela de pagos.',
        'receipt', '/billing', '["/v1/billing","/v1/expenses","/v1/invoices"]'::jsonb, '["billing"]'::jsonb, 'pro'),
    ('judicial-monitor',   'Judicial Monitor',          'integration', 'Notificaciones automaticas de procesos.',
        'Monitorea radicados en juzgados online + Rama Judicial. Notifica via inbox + email + WhatsApp cambios de estado.',
        'gavel', '/judicial', '["/v1/judicial","/v1/notifications/judicial"]'::jsonb, '[]'::jsonb, 'pro'),
    ('calendar-sync',      'Calendar Sync',             'integration', 'Sync con Google Calendar y Outlook.',
        'Sincronizacion bidireccional con Google Calendar + Outlook. Audiencias y plazos aparecen automaticamente.',
        'calendar', '/calendar', '["/v1/calendar"]'::jsonb, '["calendar_sync"]'::jsonb, 'pro'),
    ('email-ingest',       'Email Integration',         'integration', 'Lee correos relevantes y crea matters.',
        'Conecta Gmail/Outlook por OAuth. Lee emails de clientes y abre matters/timeline automaticamente.',
        'mail', '/email', '["/v1/email"]'::jsonb, '["email_ingest"]'::jsonb, 'pro'),
    ('canvas-editor',      'Canvas Editor',             'ai',          'Editor colaborativo con AI assist.',
        'Editor estilo Notion + AI sidekick. Genera, edita, redlines y exporta a PDF/Word.',
        'pen-tool', '/v2/canvas', '["/v1/canvas-transform","/v1/canvas-generate"]'::jsonb, '["canvas"]'::jsonb, 'free'),
    ('voice-agent',        'Voice Agent',               'ai',          'Asistente de voz tiempo real.',
        'Asistente OpenAI Realtime + Pinecone. Conduce intake, redacta documentos, consulta jurisprudencia en voz.',
        'mic', '/voice', '["/voice"]'::jsonb, '[]'::jsonb, 'pro'),
    ('compliance-arco',    'Compliance & Habeas Data',  'compliance',  'Gestion solicitudes ARCO + audit.',
        'Tramite solicitudes ARCO (Acceso/Rectificacion/Cancelacion/Oposicion) Ley 1581/2012. Audit completo.',
        'shield-check', '/compliance/arco', '["/v1/arco","/v1/audit"]'::jsonb, '[]'::jsonb, 'enterprise'),
    ('analytics-v2',       'Analytics Dashboard v2',    'analytics',   'KPIs + casos por etapa + cohort.',
        'Dashboard nuevo con KPIs por equipo, casos por etapa procesal, cohort de clientes, tiempo promedio por matter.',
        'bar-chart-2', '/analytics-v2', '["/v1/analytics","/v1/analytics-v2"]'::jsonb, '[]'::jsonb, 'pro'),
    ('background-agents',  'Background Agents',         'ai',          'Agentes automaticos en background.',
        'Sprint M21.S4. learn_from_seed_docs, extract_practice_patterns, derogation_sweeper, matter_summary_refresher.',
        'cpu', '/v2/agents', '["/v2/agents"]'::jsonb, '[]'::jsonb, 'enterprise')
on conflict (plugin_id) do update set
    name = excluded.name,
    category = excluded.category,
    short_description = excluded.short_description,
    long_description_md = excluded.long_description_md,
    icon = excluded.icon,
    route_path = excluded.route_path,
    api_namespaces = excluded.api_namespaces,
    requires_modules = excluded.requires_modules,
    pricing_tier = excluded.pricing_tier,
    updated_at = now();

-- Validation
do $$
declare cnt int;
begin
    select count(*) into cnt from plugin_registry;
    if cnt < 12 then
        raise exception 'M21.S6 seed validation failed: expected 12 plugins, got %', cnt;
    end if;
    raise notice 'M21.S6 OK: % plugins registrados', cnt;
end$$;
