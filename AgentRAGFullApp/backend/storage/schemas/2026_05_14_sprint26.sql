-- ============================================================
-- LexAI · Sprint 26 · Onboarding cero-fricción + Lex Helper
-- Migration date: 2026-05-14
-- Idempotent · additive · NO DROP
-- ============================================================
-- Depends on: firms, users, matters, clients (lexai_multi_tenant),
--             admin_users (Sprint 24), modules (Sprint 25)
-- ============================================================

-- ------------------------------------------------------------
-- 1. onboarding_progress · estado de checklist por firm
-- ------------------------------------------------------------
create table if not exists onboarding_progress (
  firm_id        uuid not null references firms(id) on delete cascade,
  step_key       text not null,
  status         text not null default 'pending'
    check (status in ('pending','completed','skipped')),
  completed_at   timestamptz,
  metadata       jsonb default '{}'::jsonb,
  updated_at     timestamptz default now(),
  primary key (firm_id, step_key)
);
create index if not exists onboarding_progress_firm_status_idx
  on onboarding_progress (firm_id, status);

alter table onboarding_progress enable row level security;
drop policy if exists onboarding_progress_select on onboarding_progress;
drop policy if exists onboarding_progress_modify on onboarding_progress;
create policy onboarding_progress_select on onboarding_progress for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy onboarding_progress_modify on onboarding_progress for all
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 2. helper_tips · sistema de ayuda contextual (Lex Helper)
-- ------------------------------------------------------------
create table if not exists helper_tips (
  id              uuid primary key default gen_random_uuid(),
  key             text unique not null,
  route_pattern   text,                                -- '/casos' o '/casos/%' o null = global
  module_key      text references modules(key) on delete set null,
  title           text not null,
  body            text not null,
  cta_label       text,
  cta_href        text,
  priority        int not null default 100,
  active          boolean not null default true,
  show_to_roles   text[] default null,                 -- ['lawyer','admin'] o null = todos
  category        text default 'tip'
    check (category in ('tip','feature','warning','onboarding','keyboard_shortcut')),
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);
create index if not exists helper_tips_route_idx on helper_tips (route_pattern) where active;
create index if not exists helper_tips_module_idx on helper_tips (module_key) where active;
create index if not exists helper_tips_priority_idx on helper_tips (active, priority desc);

alter table helper_tips enable row level security;
drop policy if exists helper_tips_select on helper_tips;
drop policy if exists helper_tips_modify on helper_tips;
create policy helper_tips_select on helper_tips for select using (active = true);
create policy helper_tips_modify on helper_tips for all
  using (is_saas_admin() or auth.role() = 'service_role')
  with check (is_saas_admin() or auth.role() = 'service_role');

-- ------------------------------------------------------------
-- 3. welcome_emails_log · tracking de emails de onboarding
-- ------------------------------------------------------------
create table if not exists welcome_emails_log (
  id                    bigserial primary key,
  firm_id               uuid not null references firms(id) on delete cascade,
  user_id               uuid references users(id) on delete set null,
  recipient_email       text not null,
  kind                  text not null,
  sent_at               timestamptz default now(),
  opened_at             timestamptz,
  clicked_at            timestamptz,
  provider              text default 'mock',
  provider_message_id   text,
  metadata              jsonb default '{}'::jsonb
);
create index if not exists welcome_emails_log_firm_kind_idx
  on welcome_emails_log (firm_id, kind, sent_at desc);
create unique index if not exists welcome_emails_log_dedup
  on welcome_emails_log (firm_id, kind);  -- 1 email por kind por firm (no duplicar)

alter table welcome_emails_log enable row level security;
drop policy if exists welcome_emails_log_select on welcome_emails_log;
drop policy if exists welcome_emails_log_modify on welcome_emails_log;
create policy welcome_emails_log_select on welcome_emails_log for select
  using (firm_id = auth_firm_id() or is_saas_admin() or auth.role() = 'service_role');
create policy welcome_emails_log_modify on welcome_emails_log for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- ============================================================
-- RPC · seed_demo_data · poblar firm nueva con datos de ejemplo
-- ============================================================
create or replace function lexai_seed_demo_data(p_firm_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_client_natural uuid;
  v_client_empresa uuid;
  v_matter_id uuid;
  v_already_has_matters bool;
begin
  -- Skip si la firma ya tiene matters (idempotente)
  select exists (select 1 from matters where firm_id = p_firm_id) into v_already_has_matters;
  if v_already_has_matters then
    return jsonb_build_object('skipped', true, 'reason', 'firm already has matters');
  end if;

  -- Demo client 1 · persona natural
  insert into clients (firm_id, tipo, nombre, tax_id, email, telefono, vip, metadata)
  values (
    p_firm_id, 'persona_natural', 'María Pérez (ejemplo)', '12345678',
    'maria.ejemplo@demo.lexai.co', '+57 300 000 0001', false,
    '{"is_demo": true, "seeded_by": "sprint26"}'::jsonb
  )
  returning id into v_client_natural;

  -- Demo client 2 · empresa
  insert into clients (firm_id, tipo, nombre, tax_id, email, telefono, vip, metadata)
  values (
    p_firm_id, 'persona_juridica', 'Industrias Andinas S.A.S. (ejemplo)', '900123456-7',
    'legal.ejemplo@demo.lexai.co', '+57 601 000 0002', true,
    '{"is_demo": true, "seeded_by": "sprint26"}'::jsonb
  )
  returning id into v_client_empresa;

  -- Demo matter · laboral
  insert into matters (firm_id, client_id, display_id, titulo, materia, status, priority,
                       cuantia, cuantia_currency, etapa_procesal, metadata)
  values (
    p_firm_id, v_client_natural, 'DEMO-001',
    'Liquidación laboral · ejemplo (despido sin justa causa)',
    'laboral', 'activo', 'media',
    18500000, 'COP', 'consulta_inicial',
    '{"is_demo": true, "seeded_by": "sprint26", "description": "Caso de demo · puedes eliminarlo cuando quieras"}'::jsonb
  )
  returning id into v_matter_id;

  -- Demo matter 2 · contractual con empresa
  insert into matters (firm_id, client_id, display_id, titulo, materia, status, priority, metadata)
  values (
    p_firm_id, v_client_empresa, 'DEMO-002',
    'Revisión contractual · ejemplo',
    'comercial', 'activo', 'baja',
    '{"is_demo": true, "seeded_by": "sprint26"}'::jsonb
  );

  return jsonb_build_object(
    'seeded', true,
    'firm_id', p_firm_id,
    'demo_clients', 2,
    'demo_matters', 2,
    'demo_matter_id', v_matter_id
  );
end;
$$;
grant execute on function lexai_seed_demo_data(uuid) to authenticated, service_role;

-- ============================================================
-- RPC · onboarding_state · snapshot del checklist
-- ============================================================
create or replace function lexai_onboarding_state(p_firm_id uuid default null)
returns jsonb
language sql
stable
as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id),
  steps as (
    select * from onboarding_progress where firm_id = (select id from f)
  ),
  firm_info as (
    select created_at, modo_ejercicio from firms f
      left join (select firm_id, min(modo_ejercicio) as modo_ejercicio from users group by firm_id) u
        on u.firm_id = f.id
     where f.id = (select id from f)
  ),
  signals as (
    select
      (select count(*) from matters where firm_id = (select id from f) and not coalesce((metadata->>'is_demo')::bool, false)) as real_matters_count,
      (select count(*) from clients where firm_id = (select id from f) and not coalesce((metadata->>'is_demo')::bool, false)) as real_clients_count,
      (select count(*) from matter_documents where firm_id = (select id from f)) as documents_count,
      (select count(*) from users where firm_id = (select id from f)) as users_count
  ),
  computed as (
    -- 5 pasos del checklist
    select
      jsonb_build_object('key', 'profile_complete',
        'label', 'Completa tu perfil',
        'status', case when (select modo_ejercicio from firm_info) is not null then 'completed' else 'pending' end,
        'cta_label', 'Ir a perfil',
        'cta_href', '/settings/perfil',
        'icon', 'user'
      ) as s1,
      jsonb_build_object('key', 'first_real_client',
        'label', 'Crea tu primer cliente real',
        'status', case when (select real_clients_count from signals) > 0 then 'completed' else 'pending' end,
        'cta_label', 'Nuevo cliente',
        'cta_href', '/clientes',
        'icon', 'users'
      ) as s2,
      jsonb_build_object('key', 'first_real_matter',
        'label', 'Crea tu primer caso real',
        'status', case when (select real_matters_count from signals) > 0 then 'completed' else 'pending' end,
        'cta_label', 'Nuevo caso',
        'cta_href', '/casos',
        'icon', 'folder'
      ) as s3,
      jsonb_build_object('key', 'first_document',
        'label', 'Sube tu primer documento',
        'status', case when (select documents_count from signals) > 0 then 'completed' else 'pending' end,
        'cta_label', 'Cargar documento',
        'cta_href', '/documentos',
        'icon', 'doc'
      ) as s4,
      jsonb_build_object('key', 'invite_team',
        'label', 'Invita a tu equipo',
        'status', coalesce((select status from steps where step_key = 'invite_team'),
          case when (select users_count from signals) > 1 then 'completed' else 'pending' end),
        'cta_label', 'Invitar',
        'cta_href', '/settings/usuarios',
        'icon', 'users'
      ) as s5
  ),
  step_list as (
    select array[s1, s2, s3, s4, s5] as arr from computed
  )
  select jsonb_build_object(
    'firm_id', (select id from f),
    'steps', (select to_jsonb(arr) from step_list),
    'progress_pct',
      (select (
        (s1->>'status' = 'completed')::int +
        (s2->>'status' = 'completed')::int +
        (s3->>'status' = 'completed')::int +
        (s4->>'status' = 'completed')::int +
        (s5->>'status' = 'completed')::int
      ) * 20 from computed),
    'all_completed', (select
      (s1->>'status' = 'completed') and
      (s2->>'status' = 'completed') and
      (s3->>'status' = 'completed') and
      (s4->>'status' = 'completed') and
      (s5->>'status' = 'completed')
      from computed),
    'has_demo_data', exists (
      select 1 from matters where firm_id = (select id from f)
        and coalesce((metadata->>'is_demo')::bool, false) = true
    )
  );
$$;
grant execute on function lexai_onboarding_state(uuid) to authenticated, service_role;

-- ============================================================
-- TRIGGER · seed demo data al crear firm nuevo
-- ============================================================
create or replace function lexai_on_firm_created_onboarding()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  -- Idempotente: lexai_seed_demo_data revisa si ya hay matters antes de insertar
  perform lexai_seed_demo_data(new.id);
  return new;
end;
$$;

drop trigger if exists trg_firm_seed_demo_on_create on firms;
create trigger trg_firm_seed_demo_on_create
  after insert on firms
  for each row execute function lexai_on_firm_created_onboarding();

-- ============================================================
-- SEED · helper_tips iniciales
-- ============================================================
insert into helper_tips (key, route_pattern, module_key, title, body, cta_label, cta_href, priority, category) values
  -- Globales
  ('global_voice', null, 'voice_agent',
    '¿Sabías que puedes hablarle a LexAI?',
    'Pulsa la barra de voz abajo o usa la tecla Espacio para activarla. Pídele "abre mis casos" o "redacta una contestación".',
    null, null, 50, 'feature'),

  ('global_search', null, null,
    'Búsqueda universal con ⌘K',
    'Pulsa Cmd+K (o Ctrl+K) en cualquier pantalla para abrir el buscador. Encuentra casos, clientes, documentos al instante.',
    null, null, 60, 'keyboard_shortcut'),

  -- Inicio
  ('inicio_first_visit', '/inicio', null,
    '¡Bienvenido a LexAI!',
    'Tu workspace está listo. Te sembramos 2 clientes y 2 casos de ejemplo para que explores. Cuando quieras, bórralos y crea los tuyos.',
    'Ver mis casos', '/casos', 10, 'onboarding'),

  -- Casos
  ('casos_canvas_tip', '/casos/%', 'canvas',
    'El Canvas es donde la magia sucede',
    'Dentro de un caso, abre el Canvas para redactar escritos con IA + citas verificadas. La fundamentación jurídica es lo que más valor agrega.',
    'Conocer Canvas', null, 30, 'feature'),

  ('casos_voice_tip', '/casos', 'voice_agent',
    'Habla con tus casos',
    'Di "abre el caso DEMO-001" o "muestra mis casos laborales urgentes" para navegar sin escribir.',
    null, null, 80, 'tip'),

  -- Clientes
  ('clientes_intake_tip', '/clientes', 'intake_forms',
    'Captación automática',
    'Crea un formulario de intake público y conviértelo en clientes con 1 click. Idealmente para abogados que viven de referidos.',
    'Crear formulario', '/intake-forms', 100, 'feature'),

  -- Documentos
  ('docs_drop_tip', '/documentos', 'documents',
    'Arrastra archivos directamente',
    'Arrastra cualquier PDF o Word a esta pantalla. LexAI lo procesará automáticamente · OCR + análisis + resumen.',
    null, null, 100, 'tip'),

  -- Canvas (cuando esté en un caso)
  ('canvas_citations_tip', '/casos/%/canvas', 'citations_research',
    'Citas verificadas + URL',
    'Cuando el Canvas inserta una jurisprudencia, viene con URL directa a la fuente oficial. Click para validar.',
    null, null, 50, 'feature'),

  -- Calendario
  ('calendar_court_watcher', '/calendario', 'judicial_polling',
    'Activa Court Watcher',
    'Pídenos monitorear tus expedientes y agregamos audiencias y plazos automáticamente. Disponible desde plan Pro.',
    'Ver mis planes', '/settings/billing', 100, 'feature'),

  -- Mi día
  ('mi_dia_briefing', '/mi-dia', 'daily_briefing',
    'Briefing diario',
    'Tu briefing IA del día con tareas, plazos próximos, alertas legales y oportunidades. Disponible cada mañana.',
    null, null, 90, 'feature')
on conflict (key) do nothing;

-- ============================================================
-- Backfill · marcar firms existentes como ya-tuvieron-onboarding
-- (evitar que les aparezca demo data ahora que la trigger existe)
-- ============================================================
-- No es necesario backfill explícito: lexai_seed_demo_data verifica
-- "if exists matters return", así que firms con casos no recibirán demo.

-- ============================================================
-- Done · Sprint 26 migration
-- ============================================================
