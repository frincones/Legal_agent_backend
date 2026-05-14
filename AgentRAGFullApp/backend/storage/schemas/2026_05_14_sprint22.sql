-- ============================================================
-- LexAI · Sprint 22 · M34 Client Portal B2C · Public Wizards
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- 2 tablas:
--   wizard_templates  · catálogo (system + custom por firm)
--   wizard_sessions   · cada sesión de ciudadano sin auth
-- ============================================================

-- ------------------------------------------------------------
-- 1. wizard_templates
-- System templates: firm_id = null, is_system = true
-- Custom templates por firm: firm_id != null, is_system = false
-- ------------------------------------------------------------
create table if not exists wizard_templates (
  id                uuid primary key default gen_random_uuid(),
  -- ownership
  firm_id           uuid references firms(id) on delete cascade,  -- null = system
  is_system         boolean not null default false,
  -- identificación
  slug              text not null,                                 -- 'pension-vejez', 'derecho-peticion', 'tutela-basica'
  name              text not null,
  description       text,
  category          text not null
    check (category in ('pension','derecho_peticion','tutela','contrato','denuncia','otro')),
  icon              text,                                          -- emoji o key de Ic
  -- configuración del wizard (steps + fields)
  steps             jsonb not null default '[]'::jsonb,            -- [{id, title, fields:[], help, conditions?}]
  -- plantilla del documento (mustache-like {{var}}) + secciones condicionales
  document_template text not null,
  document_title    text,                                          -- ej "DERECHO DE PETICIÓN"
  -- validaciones inline · referencia a Sprint 21 evidence validator
  identity_validations jsonb default '[]'::jsonb,                  -- [{kind:'cedula', source_field:'cedula', name_field:'nombre'}]
  -- acciones de salida disponibles
  output_actions    text[] default array['download_docx','download_pdf']::text[],
  -- routing (opcional)
  defensoria_email  text,                                          -- a quién enviar si elige Defensoría
  lead_assignee_user_id uuid references users(id) on delete set null,  -- si firma custom: a quién asignar el lead
  -- branding
  brand_color       text default 'blue',
  legal_disclaimer  text default 'Este documento fue generado con asistencia IA. No constituye representación legal. Valida con un abogado titulado antes de presentar.',
  -- estado
  active            boolean not null default true,
  sessions_count    int not null default 0,
  completions_count int not null default 0,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  -- unique constraint: slug global para system + slug per firm para custom
  unique (firm_id, slug)
);
create unique index if not exists wizard_tpl_system_slug_uq
  on wizard_templates (slug) where firm_id is null and is_system = true;
create index if not exists wizard_tpl_firm_active_idx
  on wizard_templates (firm_id, active);
create index if not exists wizard_tpl_category_idx
  on wizard_templates (category, active);

-- ------------------------------------------------------------
-- 2. wizard_sessions
-- ------------------------------------------------------------
create table if not exists wizard_sessions (
  id                uuid primary key default gen_random_uuid(),
  wizard_template_id uuid not null references wizard_templates(id) on delete cascade,
  -- anonymous session
  session_token     text not null unique,                          -- token público para resume
  -- datos
  answers           jsonb default '{}'::jsonb,
  current_step      int not null default 0,
  completed_steps   text[] default array[]::text[],
  -- identity validation (Sprint 21)
  identity_validation_id uuid references evidence_validations(id) on delete set null,
  -- output
  generated_doc_text text,
  generated_doc_url text,
  document_title    text,
  -- estado
  status            text not null default 'in_progress'
    check (status in ('in_progress','completed','submitted','abandoned','error')),
  submitted_action  text,                                          -- 'downloaded', 'emailed_defensoria', 'lead_created'
  -- routing
  routed_to_firm_id uuid references firms(id) on delete set null,
  routed_to_lead_id uuid references leads(id) on delete set null,
  routed_to_email   text,
  -- audit
  submitter_email   text,
  submitter_name    text,
  submitter_phone   text,
  ip_address        inet,
  user_agent        text,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now(),
  completed_at      timestamptz
);
create index if not exists wsess_tpl_idx
  on wizard_sessions (wizard_template_id, created_at desc);
create index if not exists wsess_status_idx
  on wizard_sessions (status, created_at desc);
create index if not exists wsess_firm_idx
  on wizard_sessions (routed_to_firm_id, created_at desc) where routed_to_firm_id is not null;

-- ============================================================
-- RLS
-- ============================================================
alter table wizard_templates enable row level security;
alter table wizard_sessions  enable row level security;

drop policy if exists wt_select on wizard_templates;
drop policy if exists wt_modify on wizard_templates;
-- Lectura: cualquier autenticado puede ver system + sus templates · service_role todo
create policy wt_select on wizard_templates for select
  using (
    (is_system = true and active = true)
    or firm_id = auth_firm_id()
    or auth.role() = 'service_role'
  );
-- Escritura: solo firm_id own templates · service_role para system seed
create policy wt_modify on wizard_templates for all
  using (
    (firm_id = auth_firm_id() and is_system = false)
    or auth.role() = 'service_role'
  )
  with check (
    (firm_id = auth_firm_id() and is_system = false)
    or auth.role() = 'service_role'
  );

drop policy if exists ws_select on wizard_sessions;
drop policy if exists ws_modify on wizard_sessions;
-- Lectura: el firm al que se enrutó la session, o service_role
create policy ws_select on wizard_sessions for select
  using (
    routed_to_firm_id = auth_firm_id()
    or auth.role() = 'service_role'
  );
create policy ws_modify on wizard_sessions for all
  using (
    routed_to_firm_id = auth_firm_id()
    or auth.role() = 'service_role'
  )
  with check (
    routed_to_firm_id = auth_firm_id()
    or auth.role() = 'service_role'
  );

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_wt_updated_at on wizard_templates;
create trigger trg_wt_updated_at before update on wizard_templates
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_ws_updated_at on wizard_sessions;
create trigger trg_ws_updated_at before update on wizard_sessions
  for each row execute function tg_set_updated_at();

-- Bump sessions_count cuando se crea + completions_count cuando completa
create or replace function tg_wsess_bump_counts() returns trigger
language plpgsql as $$
begin
  if tg_op = 'INSERT' then
    update wizard_templates
       set sessions_count = sessions_count + 1
     where id = new.wizard_template_id;
  elsif tg_op = 'UPDATE' then
    if old.status <> 'completed' and new.status = 'completed' then
      update wizard_templates
         set completions_count = completions_count + 1
       where id = new.wizard_template_id;
    end if;
  end if;
  return null;
end;
$$;
drop trigger if exists trg_wsess_counts on wizard_sessions;
create trigger trg_wsess_counts after insert or update on wizard_sessions
  for each row execute function tg_wsess_bump_counts();

-- ============================================================
-- RPCs
-- ============================================================

-- Public · trae template por slug (sin auth)
create or replace function lexai_wizard_template_by_slug(p_slug text)
returns table (
  id uuid,
  slug text,
  name text,
  description text,
  category text,
  icon text,
  steps jsonb,
  document_template text,
  document_title text,
  identity_validations jsonb,
  output_actions text[],
  brand_color text,
  legal_disclaimer text,
  is_system boolean,
  firm_id uuid
)
language sql stable as $$
  select id, slug, name, description, category, icon, steps, document_template,
         document_title, identity_validations, output_actions,
         brand_color, legal_disclaimer, is_system, firm_id
    from wizard_templates
   where slug = p_slug and active = true
   order by is_system asc                                         -- prefer firm custom over system
   limit 1;
$$;

-- Public · lista todos los wizards system activos
create or replace function lexai_wizard_system_list()
returns table (
  id uuid,
  slug text,
  name text,
  description text,
  category text,
  icon text,
  brand_color text
)
language sql stable as $$
  select id, slug, name, description, category, icon, brand_color
    from wizard_templates
   where is_system = true and active = true
   order by category, name;
$$;

-- Admin · stats por firm
create or replace function lexai_wizard_stats(p_firm_id uuid)
returns jsonb language sql stable as $$
  select jsonb_build_object(
    'templates_total', (select count(*) from wizard_templates where firm_id = p_firm_id or is_system = true),
    'templates_custom', (select count(*) from wizard_templates where firm_id = p_firm_id),
    'sessions_total', (select count(*) from wizard_sessions where routed_to_firm_id = p_firm_id),
    'sessions_completed', (select count(*) from wizard_sessions
                            where routed_to_firm_id = p_firm_id and status = 'completed'),
    'leads_created', (select count(*) from wizard_sessions
                       where routed_to_firm_id = p_firm_id and submitted_action = 'lead_created'),
    'sessions_30d', (select count(*) from wizard_sessions
                      where routed_to_firm_id = p_firm_id
                        and created_at >= now() - interval '30 days')
  );
$$;

-- ============================================================
-- Seed · 3 wizards system (idempotente)
-- ============================================================

-- 1) Derecho de petición (Art. 23 CN · Ley 1755/2015)
insert into wizard_templates
  (firm_id, is_system, slug, name, description, category, icon, steps, document_template, document_title,
   identity_validations, brand_color)
values
  (
    null, true,
    'derecho-peticion',
    'Derecho de petición',
    'Solicitud formal a entidad pública o privada · respuesta en 15 días hábiles · Ley 1755 de 2015',
    'derecho_peticion',
    '📝',
    $$[
      {
        "id": "identidad",
        "title": "Tus datos",
        "fields": [
          {"id":"nombre","label":"Nombre completo","kind":"text","required":true,"placeholder":"María González Rodríguez"},
          {"id":"cedula","label":"Cédula de ciudadanía","kind":"text","required":true,"placeholder":"52123456"},
          {"id":"direccion","label":"Dirección para notificaciones","kind":"text","required":true,"placeholder":"Cra 7 # 32-16, Bogotá"},
          {"id":"email","label":"Correo electrónico","kind":"email","required":true},
          {"id":"telefono","label":"Teléfono","kind":"phone","required":false}
        ]
      },
      {
        "id": "destinatario",
        "title": "Entidad destinataria",
        "fields": [
          {"id":"entidad","label":"Nombre de la entidad","kind":"text","required":true,"placeholder":"Ministerio de Salud y Protección Social"},
          {"id":"funcionario","label":"Funcionario o cargo (si conoces)","kind":"text","required":false,"placeholder":"Despacho del Ministro"},
          {"id":"ciudad_entidad","label":"Ciudad","kind":"text","required":true,"placeholder":"Bogotá D.C."}
        ]
      },
      {
        "id": "objeto",
        "title": "Objeto de la petición",
        "fields": [
          {"id":"asunto","label":"Asunto breve","kind":"text","required":true,"placeholder":"Solicitud de información sobre trámite de afiliación"},
          {"id":"hechos","label":"Hechos relevantes","kind":"textarea","required":true,"placeholder":"Describe los hechos de manera clara y cronológica..."},
          {"id":"peticion","label":"Lo que solicitas concretamente","kind":"textarea","required":true,"placeholder":"Solicito a esa entidad que..."}
        ]
      }
    ]$$::jsonb,
    $$Señores
{{entidad}}{{#funcionario}}
{{funcionario}}{{/funcionario}}
{{ciudad_entidad}}

ASUNTO: Derecho de petición — {{asunto}}

Respetados señores:

Yo, {{nombre}}, mayor de edad, identificado(a) con cédula de ciudadanía
número {{cedula}}, domiciliado(a) en {{direccion}}, en ejercicio del
derecho fundamental de petición consagrado en el artículo 23 de la
Constitución Política y reglamentado por la Ley 1755 de 2015,
respetuosamente formulo la siguiente petición:

HECHOS

{{hechos}}

PETICIÓN

{{peticion}}

NOTIFICACIONES

Solicito que las respuestas y comunicaciones se notifiquen a la
dirección {{direccion}} o al correo electrónico {{email}}{{#telefono}},
o al teléfono {{telefono}}{{/telefono}}.

FUNDAMENTOS JURÍDICOS

Esta solicitud se eleva con fundamento en el artículo 23 de la
Constitución Política, la Ley 1755 de 2015 que regula el derecho de
petición, y demás normas concordantes. Recuerdo a esa entidad que
cuenta con quince (15) días hábiles para responder de fondo y de manera
clara, según el artículo 14 de la Ley 1755 de 2015.

Cordialmente,


________________________________
{{nombre}}
C.C. {{cedula}}{{#email}}
Correo: {{email}}{{/email}}{{#telefono}}
Teléfono: {{telefono}}{{/telefono}}
$$,
    'DERECHO DE PETICIÓN',
    '[{"kind":"cedula","source_field":"cedula","name_field":"nombre"}]'::jsonb,
    'blue'
  )
on conflict do nothing;

-- 2) Tutela básica (Art. 86 CN · Decreto 2591/1991)
insert into wizard_templates
  (firm_id, is_system, slug, name, description, category, icon, steps, document_template, document_title,
   identity_validations, brand_color)
values
  (
    null, true,
    'tutela-basica',
    'Acción de tutela',
    'Protección de derechos fundamentales vulnerados o amenazados · Art. 86 CN · Decreto 2591/1991',
    'tutela',
    '⚖️',
    $$[
      {
        "id": "identidad",
        "title": "Tus datos",
        "fields": [
          {"id":"nombre","label":"Nombre completo del accionante","kind":"text","required":true},
          {"id":"cedula","label":"Cédula","kind":"text","required":true},
          {"id":"direccion","label":"Dirección","kind":"text","required":true},
          {"id":"telefono","label":"Teléfono","kind":"phone","required":true},
          {"id":"email","label":"Correo","kind":"email","required":true}
        ]
      },
      {
        "id": "accionada",
        "title": "Entidad accionada",
        "fields": [
          {"id":"accionada_nombre","label":"Nombre de la entidad o persona accionada","kind":"text","required":true},
          {"id":"accionada_direccion","label":"Dirección de la entidad","kind":"text","required":false},
          {"id":"accionada_email","label":"Correo de la entidad","kind":"email","required":false}
        ]
      },
      {
        "id": "derechos",
        "title": "Derechos vulnerados",
        "fields": [
          {"id":"derechos_vulnerados","label":"¿Qué derechos fundamentales sientes vulnerados?","kind":"select","required":true,
           "options":["Vida","Salud","Mínimo vital","Petición","Debido proceso","Igualdad","Educación","Vivienda digna","Otro"]},
          {"id":"derechos_otro","label":"Si elegiste 'Otro', especifica","kind":"text","required":false}
        ]
      },
      {
        "id": "hechos",
        "title": "Hechos",
        "fields": [
          {"id":"fecha_hechos","label":"Fecha aproximada de los hechos","kind":"date","required":true},
          {"id":"hechos_detalle","label":"Describe los hechos de manera cronológica","kind":"textarea","required":true}
        ]
      },
      {
        "id": "pretensiones",
        "title": "Lo que pides al juez",
        "fields": [
          {"id":"pretension_principal","label":"Pretensión principal","kind":"textarea","required":true,
           "placeholder":"Ordenar a la entidad accionada que..."}
        ]
      }
    ]$$::jsonb,
    $$Señor(a) Juez

ACCIÓN DE TUTELA

ACCIONANTE: {{nombre}}, C.C. {{cedula}}
ACCIONADA: {{accionada_nombre}}
DERECHO(S) VULNERADO(S): {{derechos_vulnerados}}{{#derechos_otro}} · {{derechos_otro}}{{/derechos_otro}}

Honorable Juez:

{{nombre}}, mayor de edad, identificado(a) con cédula de ciudadanía
{{cedula}}, con domicilio en {{direccion}}, en uso del derecho consagrado
en el artículo 86 de la Constitución Política y desarrollado por el
Decreto 2591 de 1991, ante usted respetuosamente comparezco para
interponer ACCIÓN DE TUTELA contra {{accionada_nombre}}, con fundamento
en los siguientes:

HECHOS

El día {{fecha_hechos}} ocurrió lo siguiente:

{{hechos_detalle}}

DERECHOS FUNDAMENTALES VULNERADOS

Considero vulnerado el derecho fundamental a {{derechos_vulnerados}}{{#derechos_otro}}
y el derecho a {{derechos_otro}}{{/derechos_otro}}, consagrado(s) en la
Constitución Política de Colombia.

PRETENSIONES

Solicito al señor(a) Juez:

{{pretension_principal}}

JURAMENTO

Bajo la gravedad del juramento manifiesto que no he interpuesto otra
acción de tutela por los mismos hechos y derechos contra la misma
entidad accionada.

NOTIFICACIONES

Accionante: {{direccion}}, correo {{email}}, teléfono {{telefono}}.{{#accionada_direccion}}
Accionada: {{accionada_direccion}}{{#accionada_email}}, correo {{accionada_email}}{{/accionada_email}}.{{/accionada_direccion}}

ANEXOS

Los documentos pertinentes se aportarán dentro de las 48 horas
siguientes a la radicación de la presente acción.

Atentamente,


________________________________
{{nombre}}
C.C. {{cedula}}
$$,
    'ACCIÓN DE TUTELA',
    '[{"kind":"cedula","source_field":"cedula","name_field":"nombre"}]'::jsonb,
    'purple'
  )
on conflict do nothing;

-- 3) Solicitud de pensión por vejez (Ley 100/1993 · Régimen Prima Media)
insert into wizard_templates
  (firm_id, is_system, slug, name, description, category, icon, steps, document_template, document_title,
   identity_validations, brand_color)
values
  (
    null, true,
    'pension-vejez',
    'Solicitud de pensión de vejez',
    'Reconocimiento de pensión por vejez · Ley 100/1993 · Régimen de Prima Media (Colpensiones)',
    'pension',
    '👴',
    $$[
      {
        "id": "identidad",
        "title": "Tus datos",
        "fields": [
          {"id":"nombre","label":"Nombre completo","kind":"text","required":true},
          {"id":"cedula","label":"Cédula","kind":"text","required":true},
          {"id":"fecha_nacimiento","label":"Fecha de nacimiento","kind":"date","required":true},
          {"id":"direccion","label":"Dirección","kind":"text","required":true},
          {"id":"email","label":"Correo","kind":"email","required":true},
          {"id":"telefono","label":"Teléfono","kind":"phone","required":true}
        ]
      },
      {
        "id": "regimen",
        "title": "Régimen pensional",
        "fields": [
          {"id":"regimen","label":"¿En qué régimen estás?","kind":"select","required":true,
           "options":["Prima Media (Colpensiones)","Ahorro Individual (AFP privada)"]},
          {"id":"fondo","label":"Nombre del fondo (si AFP privada)","kind":"text","required":false}
        ]
      },
      {
        "id": "semanas",
        "title": "Semanas cotizadas",
        "fields": [
          {"id":"semanas_cotizadas","label":"Semanas cotizadas (aproximado)","kind":"number","required":true,"min":0,"max":3000},
          {"id":"primer_empleo","label":"Año de tu primer empleo formal","kind":"number","required":true,"min":1950,"max":2025}
        ]
      },
      {
        "id": "extras",
        "title": "Información adicional",
        "fields": [
          {"id":"observaciones","label":"Observaciones (opcional)","kind":"textarea","required":false}
        ]
      }
    ]$$::jsonb,
    $$Señores
{{#regimen_es_prima_media}}ADMINISTRADORA COLOMBIANA DE PENSIONES — COLPENSIONES{{/regimen_es_prima_media}}{{#regimen_es_afp}}{{fondo}}{{/regimen_es_afp}}

ASUNTO: Solicitud de reconocimiento y pago de pensión de vejez — {{nombre}} C.C. {{cedula}}

Respetados señores:

Yo, {{nombre}}, mayor de edad, identificado(a) con cédula de ciudadanía
{{cedula}}, nacido(a) el {{fecha_nacimiento}}, con domicilio en
{{direccion}}, respetuosamente solicito a esa Administradora el
RECONOCIMIENTO Y PAGO DE PENSIÓN DE VEJEZ, con fundamento en los
siguientes:

HECHOS

1. Cumplo los requisitos establecidos en el artículo 33 de la Ley 100
   de 1993 (modificado por el artículo 9 de la Ley 797 de 2003) para
   acceder a la pensión de vejez en el régimen {{regimen}}.

2. Inicié mi vida laboral en el año {{primer_empleo}} y a la fecha
   cuento con aproximadamente {{semanas_cotizadas}} semanas cotizadas
   al sistema general de pensiones.

3. He realizado los aportes correspondientes en los términos legales y
   actualmente reúno los requisitos de edad y tiempo de cotización
   exigidos por la ley.

{{#observaciones}}OBSERVACIONES

{{observaciones}}

{{/observaciones}}PETICIÓN

PRIMERO: Reconocer y ordenar el pago de la pensión de vejez a la que
tengo derecho, conforme a la Ley 100 de 1993 y normas concordantes.

SEGUNDO: Liquidar las mesadas pensionales conforme al ingreso base de
liquidación que corresponda según los aportes realizados.

TERCERO: Disponer las novedades necesarias ante el sistema general de
pensiones para hacer efectivo el reconocimiento.

NOTIFICACIONES

Recibiré notificaciones en {{direccion}}, correo electrónico {{email}}
o teléfono {{telefono}}.

FUNDAMENTOS JURÍDICOS

Artículos 33 y 36 de la Ley 100 de 1993 · artículo 9 de la Ley 797 de 2003 ·
artículo 48 de la Constitución Política · sentencias C-258/2013,
T-1093/2008 y demás concordantes de la Corte Constitucional.

Cordialmente,


________________________________
{{nombre}}
C.C. {{cedula}}
{{#email}}{{email}}{{/email}}{{#telefono}} · {{telefono}}{{/telefono}}
$$,
    'SOLICITUD DE PENSIÓN DE VEJEZ',
    '[{"kind":"cedula","source_field":"cedula","name_field":"nombre"}]'::jsonb,
    'green'
  )
on conflict do nothing;
