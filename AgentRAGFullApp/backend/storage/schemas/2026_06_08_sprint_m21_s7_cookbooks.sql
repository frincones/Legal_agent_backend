-- Sprint M21.S7 · Managed Cookbooks
--
-- Cookbooks = recetas declarativas E2E que orquestan tools + skills + connectors
-- en flujos guiados (due-diligence, demanda laboral, contrato fiducia, etc.).
--
-- Tablas:
--   - cookbook_registry · catalogo builtin (igual patron que plugin_registry)
--   - cookbook_runs     · cada ejecucion (audit + step results)
--   - cookbook_step_logs · log granular por step
--
-- Multi-tenant: cookbook_registry es shared, runs y step_logs son por firm.

create table if not exists cookbook_registry (
    cookbook_id     text primary key,                -- 'due_diligence_co', 'demanda_laboral_co'
    name            text not null,
    category        text not null,                   -- corporate|litigation|notarial|transactional
    short_description text not null,
    long_description_md text,
    icon            text,
    inputs_schema   jsonb not null default '{}'::jsonb,  -- JSON Schema de inputs
    steps           jsonb not null default '[]'::jsonb,  -- array declarativo de pasos
    estimated_minutes integer default 5,
    pricing_tier    text not null default 'free',
    requires_modules jsonb not null default '[]'::jsonb,
    builtin         boolean not null default true,
    version         text not null default '1.0.0',
    documentation_url text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists cookbook_runs (
    run_id          uuid primary key default gen_random_uuid(),
    firm_id         uuid not null,
    cookbook_id     text not null references cookbook_registry(cookbook_id) on delete restrict,
    matter_id       uuid,                            -- opcional vincular a matter_workspace
    started_by_user_id uuid,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    duration_ms     integer,
    status          text not null default 'running', -- 'running'|'ok'|'error'|'cancelled'
    inputs          jsonb not null default '{}'::jsonb,
    outputs         jsonb not null default '{}'::jsonb,
    error_message   text,
    cost_usd        numeric(10,4),
    metadata        jsonb not null default '{}'::jsonb
);
create index if not exists ix_cookbook_runs_firm_started on cookbook_runs(firm_id, started_at desc);
create index if not exists ix_cookbook_runs_status on cookbook_runs(status) where status='running';

create table if not exists cookbook_step_logs (
    log_id          bigserial primary key,
    run_id          uuid not null references cookbook_runs(run_id) on delete cascade,
    firm_id         uuid not null,
    step_index      integer not null,
    step_name       text not null,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    duration_ms     integer,
    status          text not null,
    output          jsonb default '{}'::jsonb,
    error_message   text
);
create index if not exists ix_cookbook_step_logs_run on cookbook_step_logs(run_id, step_index);

-- RLS
alter table cookbook_runs enable row level security;
alter table cookbook_step_logs enable row level security;
do $$
begin
    if not exists (select 1 from pg_policies where tablename='cookbook_runs' and policyname='cookbook_runs_rls_select') then
        create policy cookbook_runs_rls_select on cookbook_runs for select using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='cookbook_runs' and policyname='cookbook_runs_rls_all') then
        create policy cookbook_runs_rls_all on cookbook_runs for all
            using (firm_id = current_user_firm_id())
            with check (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='cookbook_step_logs' and policyname='cookbook_step_logs_rls_select') then
        create policy cookbook_step_logs_rls_select on cookbook_step_logs for select using (firm_id = current_user_firm_id());
    end if;
    if not exists (select 1 from pg_policies where tablename='cookbook_step_logs' and policyname='cookbook_step_logs_rls_insert') then
        create policy cookbook_step_logs_rls_insert on cookbook_step_logs for insert with check (firm_id = current_user_firm_id());
    end if;
end$$;

-- ─── Seed: 5 cookbooks builtin ───────────────────────────────
insert into cookbook_registry
    (cookbook_id, name, category, short_description, long_description_md, icon,
     inputs_schema, steps, estimated_minutes, pricing_tier, version)
values
    ('due_diligence_co', 'Due Diligence Corporativo CO', 'corporate',
     'Verificacion completa de existencia, representacion y estado fiscal de una empresa.',
     '## Due Diligence Corporativo\n\nValida en paralelo:\n- Existencia y representacion (RUES)\n- NIT activo (DIAN)\n- Procesos judiciales en curso (Rama Judicial)\n- Multas/sanciones (Funcion Publica)\n\nGenera un dictamen consolidado con findings + recomendaciones.',
     'search-check',
     '{"type":"object","properties":{"nit":{"type":"string","description":"NIT de la empresa"},"razon_social":{"type":"string"}},"required":["nit"]}'::jsonb,
     '[
        {"name":"load_company_profile","tool":"load_company_profile","inputs":{}},
        {"name":"verify_rues","connector":"rues","query":"{nit}"},
        {"name":"check_dian","connector":"dian","query":"{nit}"},
        {"name":"check_judicial","connector":"csj_juzgados_online","query":"{razon_social}"},
        {"name":"generate_dictamen","tool":"generate_clause","section":"dictamen_due_diligence"},
        {"name":"apply_guardrails","tool":"apply_guardrails","destination":"client"}
     ]'::jsonb,
     8, 'pro', '1.0.0'),

    ('demanda_laboral_co', 'Demanda Laboral CO', 'litigation',
     'Demanda ordinaria laboral con calculo de prestaciones e intereses.',
     '## Demanda Laboral\n\nFlujo completo:\n1. Load SKILL demanda_laboral\n2. Calculo de prestaciones (cesantias, primas, vacaciones, intereses)\n3. Verificacion de citas laborales\n4. Genera secciones (hechos, pretensiones, fundamentos)\n5. Build DOCX final',
     'gavel',
     '{"type":"object","properties":{"empleador":{"type":"string"},"trabajador":{"type":"string"},"salario_mensual":{"type":"number"},"fecha_ingreso":{"type":"string"},"fecha_retiro":{"type":"string"},"motivo":{"type":"string"}},"required":["empleador","trabajador","salario_mensual","fecha_ingreso"]}'::jsonb,
     '[
        {"name":"load_skill_md","tool":"load_skill_md","inputs":{"doc_type":"demanda_laboral"}},
        {"name":"calc_prestaciones","tool":"calc_legal","inputs":{"kind":"liquidacion_laboral"}},
        {"name":"verify_citations","tool":"verify_citation","inputs":{"max":5}},
        {"name":"generate_hechos","tool":"generate_clause","section":"hechos"},
        {"name":"generate_pretensiones","tool":"generate_clause","section":"pretensiones"},
        {"name":"generate_fundamentos","tool":"generate_clause","section":"fundamentos_derecho"},
        {"name":"check_completeness","tool":"check_completeness","inputs":{}},
        {"name":"apply_guardrails","tool":"apply_guardrails","destination":"court"},
        {"name":"build_docx","tool":"build_docx","inputs":{}}
     ]'::jsonb,
     15, 'pro', '1.0.0'),

    ('contrato_fiducia_co', 'Contrato de Fiducia Mercantil CO', 'transactional',
     'Contrato de fiducia mercantil ajustado al Codigo de Comercio CO.',
     '## Contrato de Fiducia\n\nGenera contrato completo con clausulas estandar de fiducia (Art. 1226 CCo).',
     'file-signature',
     '{"type":"object","properties":{"fiduciante":{"type":"string"},"fiduciario":{"type":"string"},"beneficiarios":{"type":"array","items":{"type":"string"}},"objeto":{"type":"string"},"valor":{"type":"number"}},"required":["fiduciante","fiduciario","objeto"]}'::jsonb,
     '[
        {"name":"load_skill_md","tool":"load_skill_md","inputs":{"doc_type":"contrato_fiducia"}},
        {"name":"load_playbook","tool":"load_playbook","inputs":{"area":"contractual"}},
        {"name":"generate_partes","tool":"generate_clause","section":"partes_contratantes"},
        {"name":"generate_objeto","tool":"generate_clause","section":"objeto"},
        {"name":"generate_clausulas","tool":"generate_clause","section":"clausulado"},
        {"name":"verify_citations","tool":"verify_citation","inputs":{}},
        {"name":"check_completeness","tool":"check_completeness","inputs":{}},
        {"name":"build_docx","tool":"build_docx","inputs":{}}
     ]'::jsonb,
     12, 'pro', '1.0.0'),

    ('oferta_mercantil_co', 'Oferta Mercantil CO', 'transactional',
     'Oferta mercantil con aceptacion + perfeccionamiento (Art. 845 CCo).',
     '## Oferta Mercantil\n\nGenera oferta + carta aceptacion + clausulas de perfeccionamiento.',
     'handshake',
     '{"type":"object","properties":{"oferente":{"type":"string"},"destinatario":{"type":"string"},"bienes_servicios":{"type":"string"},"precio":{"type":"number"},"plazo_aceptacion_dias":{"type":"integer","default":15}},"required":["oferente","destinatario","bienes_servicios"]}'::jsonb,
     '[
        {"name":"load_skill_md","tool":"load_skill_md","inputs":{"doc_type":"oferta_mercantil"}},
        {"name":"generate_oferta","tool":"generate_clause","section":"oferta"},
        {"name":"generate_aceptacion","tool":"generate_clause","section":"carta_aceptacion"},
        {"name":"verify_citations","tool":"verify_citation","inputs":{}},
        {"name":"build_docx","tool":"build_docx","inputs":{}}
     ]'::jsonb,
     8, 'free', '1.0.0'),

    ('escritura_compraventa_co', 'Escritura de Compraventa CO', 'notarial',
     'Escritura publica de compraventa inmueble (con verificacion catastral IGAC).',
     '## Escritura Compraventa\n\nFlujo notarial completo:\n1. Verificacion catastral (IGAC)\n2. Matricula inmobiliaria (SNR)\n3. Estado tradicion + libertad\n4. Genera escritura con clausulas Art. 1857 CC',
     'home',
     '{"type":"object","properties":{"vendedor":{"type":"string"},"comprador":{"type":"string"},"matricula_inmobiliaria":{"type":"string"},"cedula_catastral":{"type":"string"},"precio":{"type":"number"},"direccion":{"type":"string"}},"required":["vendedor","comprador","matricula_inmobiliaria"]}'::jsonb,
     '[
        {"name":"load_skill_md","tool":"load_skill_md","inputs":{"doc_type":"escritura_compraventa"}},
        {"name":"verify_catastro","connector":"igac","query":"{cedula_catastral}"},
        {"name":"verify_snr","connector":"snr","query":"{matricula_inmobiliaria}"},
        {"name":"generate_comparecencia","tool":"generate_clause","section":"comparecencia"},
        {"name":"generate_inmueble","tool":"generate_clause","section":"identificacion_inmueble"},
        {"name":"generate_clausulas","tool":"generate_clause","section":"clausulas_compraventa"},
        {"name":"apply_guardrails","tool":"apply_guardrails","destination":"client"},
        {"name":"build_docx","tool":"build_docx","inputs":{}}
     ]'::jsonb,
     20, 'pro', '1.0.0')
on conflict (cookbook_id) do update set
    name = excluded.name, category = excluded.category,
    short_description = excluded.short_description,
    long_description_md = excluded.long_description_md,
    icon = excluded.icon,
    inputs_schema = excluded.inputs_schema,
    steps = excluded.steps,
    estimated_minutes = excluded.estimated_minutes,
    pricing_tier = excluded.pricing_tier,
    version = excluded.version,
    updated_at = now();

do $$
declare cnt int;
begin
    select count(*) into cnt from cookbook_registry;
    if cnt < 5 then
        raise exception 'M21.S7 seed validation failed: expected 5 cookbooks, got %', cnt;
    end if;
    raise notice 'M21.S7 OK: % cookbooks registrados', cnt;
end$$;
