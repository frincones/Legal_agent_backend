-- Sprint M21.S5 · MCP Connectors Registry
--
-- Catalogo de 20+ conectores oficiales colombianos + health monitoring.
-- Multi-tenant via firm_mcp_subscriptions (cual firm uso cual connector + ultima vez).

create table if not exists mcp_connectors_registry (
    connector_id    text primary key,                -- 'corte_cc', 'csj', 'dian', etc.
    name            text not null,                   -- 'Corte Constitucional'
    category        text not null,                   -- judicial|normativo|fiscal|propiedad|administrativo|registro
    description     text not null,
    jurisdiction    text not null default 'CO',
    base_url        text,
    api_kind        text not null default 'http',    -- 'http'|'scrape'|'graphql'|'mcp'
    auth_required   boolean not null default false,
    rate_limit_rps  integer default 1,
    avg_latency_ms  integer,
    enabled         boolean not null default true,
    tags            jsonb not null default '[]'::jsonb,
    config_schema   jsonb not null default '{}'::jsonb,
    documentation_url text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists connector_health (
    health_id       bigserial primary key,
    connector_id    text not null references mcp_connectors_registry(connector_id) on delete cascade,
    checked_at      timestamptz not null default now(),
    status          text not null,                    -- 'up'|'down'|'degraded'
    latency_ms      integer,
    error_message   text,
    metadata        jsonb default '{}'::jsonb
);
create index if not exists ix_conn_health_id_time on connector_health(connector_id, checked_at desc);

create table if not exists firm_mcp_subscriptions (
    firm_id         uuid not null,
    connector_id    text not null references mcp_connectors_registry(connector_id) on delete cascade,
    enabled         boolean not null default true,
    last_used_at    timestamptz,
    use_count       integer not null default 0,
    config          jsonb not null default '{}'::jsonb,
    primary key (firm_id, connector_id)
);

-- RLS multi-tenant (solo en firm_mcp_subscriptions; registry y health son shared/admin)
alter table firm_mcp_subscriptions enable row level security;
do $$
begin
    if not exists (select 1 from pg_policies where tablename='firm_mcp_subscriptions' and policyname='firm_mcp_subs_rls_select') then
        create policy firm_mcp_subs_rls_select on firm_mcp_subscriptions for select
            using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='firm_mcp_subscriptions' and policyname='firm_mcp_subs_rls_all') then
        create policy firm_mcp_subs_rls_all on firm_mcp_subscriptions for all
            using (firm_id = current_user_firm_id())
            with check (firm_id = current_user_firm_id());
    end if;
end$$;

-- ─── Seed: 20 connectors builtin (idempotente via on conflict do update) ─────
insert into mcp_connectors_registry
    (connector_id, name, category, description, jurisdiction, base_url, api_kind, auth_required, rate_limit_rps, tags)
values
-- Judiciales (5)
    ('corte_cc',          'Corte Constitucional',              'judicial', 'Sentencias y autos de la Corte Constitucional CO.', 'CO', 'https://www.corteconstitucional.gov.co', 'scrape', false, 1, '["sentencias","tutela","constitucional"]'),
    ('csj',               'Corte Suprema de Justicia',         'judicial', 'Sentencias civiles, laborales y penales de la CSJ.',  'CO', 'https://cortesuprema.gov.co',          'scrape', false, 1, '["casacion","civil","laboral","penal"]'),
    ('ce',                'Consejo de Estado',                  'judicial', 'Jurisprudencia contencioso-administrativa.',         'CO', 'https://www.consejodeestado.gov.co',   'scrape', false, 1, '["administrativo","tributario"]'),
    ('csj_juzgados_online','Juzgados Online (Rama Judicial)',   'judicial', 'Consulta de procesos en linea Rama Judicial.',       'CO', 'https://consultaprocesos.ramajudicial.gov.co', 'http', false, 2, '["expedientes","radicado"]'),
    ('jep',               'Jurisdiccion Especial para la Paz', 'judicial', 'Sentencias y autos JEP.',                            'CO', 'https://www.jep.gov.co',               'scrape', false, 1, '["transicional","paz"]'),

-- Normativos (5)
    ('suin',              'SUIN-Juriscol',                     'normativo','Sistema unificado de informacion normativa.',         'CO', 'https://www.suin-juriscol.gov.co',     'scrape', false, 2, '["leyes","decretos","derogacion"]'),
    ('senado',            'Secretaria del Senado',             'normativo','Leyes en tramite + sancionadas + gacetas.',          'CO', 'https://www.secretariasenado.gov.co',  'scrape', false, 1, '["leyes","gacetas"]'),
    ('camara',            'Camara de Representantes',          'normativo','Proyectos de ley y debates.',                        'CO', 'https://www.camara.gov.co',            'scrape', false, 1, '["proyectos_ley"]'),
    ('diario_oficial',    'Diario Oficial',                    'normativo','Publicacion oficial de normas.',                     'CO', 'https://www.diariooficial.gov.co',     'scrape', false, 1, '["publicacion_oficial"]'),
    ('funcpub',           'Funcion Publica',                   'normativo','Conceptos juridicos + normograma.',                  'CO', 'https://www.funcionpublica.gov.co',    'scrape', false, 1, '["conceptos","normograma"]'),

-- Fiscales (3)
    ('dian',              'DIAN',                              'fiscal',   'Consulta NIT, RUT, conceptos tributarios.',          'CO', 'https://www.dian.gov.co',              'http',   false, 2, '["nit","rut","tributario"]'),
    ('superfinanciera',   'Superintendencia Financiera',       'fiscal',   'Conceptos juridicos sector financiero.',             'CO', 'https://www.superfinanciera.gov.co',   'scrape', false, 1, '["conceptos","financiero"]'),
    ('rues',              'RUES (Camaras de Comercio)',        'fiscal',   'Registro Unico Empresarial - existencia empresas.',  'CO', 'https://www.rues.org.co',              'http',   false, 2, '["empresa","camara_comercio"]'),

-- Propiedad / registro (3)
    ('igac',              'IGAC (Geografico)',                 'propiedad','Catastro nacional - cedula catastral.',              'CO', 'https://geoportal.igac.gov.co',        'http',   false, 1, '["catastro","predio"]'),
    ('snr',               'SNR Registro de Instrumentos',      'registro', 'Matriculas inmobiliarias y registros publicos.',     'CO', 'https://snrbotonpago.gov.co',          'scrape', false, 1, '["matricula","registro_inmobiliario"]'),
    ('ica',               'ICA (Agropecuario)',                'propiedad','Registros sanitarios agropecuarios.',                'CO', 'https://www.ica.gov.co',               'scrape', false, 1, '["agropecuario","ica"]'),

-- Administrativos (4)
    ('dafp',              'DAFP - Funcion Publica',            'administrativo','Conceptos sobre empleo publico y carrera.',     'CO', 'https://www.funcionpublica.gov.co',    'scrape', false, 1, '["empleo_publico","carrera"]'),
    ('minjusticia',       'Ministerio de Justicia',            'administrativo','Resoluciones y conceptos del Ministerio.',      'CO', 'https://www.minjusticia.gov.co',       'scrape', false, 1, '["resoluciones","conceptos"]'),
    ('sicaac',            'SICAAC (Conciliacion)',             'administrativo','Centros de conciliacion + arbitraje.',          'CO', 'https://sicaac.gov.co',                'scrape', false, 1, '["conciliacion","arbitraje"]'),
    ('datos_gov',         'datos.gov.co (Socrata)',            'administrativo','Datos abiertos del gobierno (Socrata API).',    'CO', 'https://www.datos.gov.co',             'http',   false, 5, '["datos_abiertos","socrata"]')
on conflict (connector_id) do update set
    name = excluded.name,
    category = excluded.category,
    description = excluded.description,
    base_url = excluded.base_url,
    api_kind = excluded.api_kind,
    auth_required = excluded.auth_required,
    rate_limit_rps = excluded.rate_limit_rps,
    tags = excluded.tags,
    updated_at = now();

-- Validation
do $$
declare cnt int;
begin
    select count(*) into cnt from mcp_connectors_registry;
    if cnt < 20 then
        raise exception 'M21.S5 seed validation failed: expected 20 connectors, got %', cnt;
    end if;
    raise notice 'M21.S5 OK: % connectors registrados', cnt;
end$$;
