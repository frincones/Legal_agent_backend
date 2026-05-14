-- ============================================================
-- LexAI · Sprint 14 · API pública + Webhooks salientes + Marketplace
-- Migration date: 2026-05-13
-- Idempotent · additive · NO DROP
-- ============================================================

-- ------------------------------------------------------------
-- 1. api_keys · llaves de API por firma con scopes y rate limiting
-- ------------------------------------------------------------
create table if not exists api_keys (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  prefix            text not null,                          -- 'lex_live_XXXX' visible (8 chars)
  key_hash          text not null unique,                   -- sha256 hex de la key completa
  scopes            text[] not null default array['read']::text[],
                                                            -- 'read','write','admin','matters','clients','leads',etc.
  rate_limit_per_min int not null default 60,
  active            boolean not null default true,
  expires_at        timestamptz,
  last_used_at      timestamptz,
  last_used_ip      inet,
  use_count         int not null default 0,
  revoked_at        timestamptz,
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now()
);
create index if not exists api_keys_firm_idx
  on api_keys (firm_id, active) where active = true and revoked_at is null;
create index if not exists api_keys_prefix_idx
  on api_keys (prefix);

-- ------------------------------------------------------------
-- 2. api_key_usage_log · audit por request
-- ------------------------------------------------------------
create table if not exists api_key_usage_log (
  id                bigserial primary key,
  firm_id           uuid not null references firms(id) on delete cascade,
  api_key_id        uuid not null references api_keys(id) on delete cascade,
  endpoint          text not null,
  method            text not null,
  status_code       int,
  duration_ms       int,
  ip_address        inet,
  user_agent        text,
  occurred_at       timestamptz default now()
);
create index if not exists api_usage_key_time_idx
  on api_key_usage_log (api_key_id, occurred_at desc);
create index if not exists api_usage_firm_time_idx
  on api_key_usage_log (firm_id, occurred_at desc);

-- ------------------------------------------------------------
-- 3. outbound_webhooks · subscripciones para eventos salientes
-- ------------------------------------------------------------
create table if not exists outbound_webhooks (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  name              text not null,
  url               text not null,
  secret            text not null,                          -- shared HMAC secret
  events            text[] not null default array[]::text[],
                                                            -- ['matter.created','deadline.due_soon','lead.converted',...]
  active            boolean not null default true,
  last_delivery_at  timestamptz,
  last_status_code  int,
  success_count     int not null default 0,
  failure_count     int not null default 0,
  metadata          jsonb default '{}'::jsonb,
  created_by        uuid references users(id) on delete set null,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists webhooks_firm_active_idx
  on outbound_webhooks (firm_id, active) where active = true;
create index if not exists webhooks_events_idx
  on outbound_webhooks using gin (events);

-- ------------------------------------------------------------
-- 4. webhook_deliveries · log de entregas con retry backoff
-- ------------------------------------------------------------
create table if not exists webhook_deliveries (
  id                uuid primary key default gen_random_uuid(),
  firm_id           uuid not null references firms(id) on delete cascade,
  webhook_id        uuid not null references outbound_webhooks(id) on delete cascade,
  event_type        text not null,
  event_id          text not null,                          -- idempotency key
  payload           jsonb not null,
  status            text not null default 'pending'
    check (status in ('pending','retrying','succeeded','failed','dropped')),
  status_code       int,
  response_body     text,
  attempt_count     int not null default 0,
  max_attempts      int not null default 5,
  next_retry_at     timestamptz,
  succeeded_at      timestamptz,
  failed_at         timestamptz,
  error             text,
  created_at        timestamptz default now(),
  unique (webhook_id, event_id)
);
create index if not exists wd_pending_idx
  on webhook_deliveries (next_retry_at) where status in ('pending','retrying');
create index if not exists wd_webhook_time_idx
  on webhook_deliveries (webhook_id, created_at desc);

-- ------------------------------------------------------------
-- 5. template_marketplace_items · plantillas compartidas
-- ------------------------------------------------------------
create table if not exists template_marketplace_items (
  id                uuid primary key default gen_random_uuid(),
  author_firm_id    uuid references firms(id) on delete set null,
  author_user_id    uuid references users(id) on delete set null,
  name              text not null,
  doc_type          text not null,                          -- 'tutela','contestacion','contrato',etc.
  category          text not null default 'general',        -- 'laboral','civil','administrativo','familia','penal','comercial','general'
  jurisdiction      text default 'colombia',
  description       text,
  body              text not null,                          -- markdown / docx-compatible
  variables         text[] default array[]::text[],
  visibility        text not null default 'public'
    check (visibility in ('public','firm_only','pending_review','rejected')),
  downloads         int not null default 0,
  stars             int not null default 0,
  forks             int not null default 0,
  is_official       boolean not null default false,         -- creado por LexAI team
  metadata          jsonb default '{}'::jsonb,
  created_at        timestamptz default now(),
  updated_at        timestamptz default now()
);
create index if not exists mp_items_visible_idx
  on template_marketplace_items (visibility, doc_type, category) where visibility = 'public';
create index if not exists mp_items_popular_idx
  on template_marketplace_items (stars desc, downloads desc) where visibility = 'public';
create index if not exists mp_items_author_idx
  on template_marketplace_items (author_firm_id) where author_firm_id is not null;

-- ------------------------------------------------------------
-- 6. template_marketplace_stars · stars por usuario
-- ------------------------------------------------------------
create table if not exists template_marketplace_stars (
  item_id           uuid not null references template_marketplace_items(id) on delete cascade,
  user_id           uuid not null references users(id) on delete cascade,
  firm_id           uuid not null references firms(id) on delete cascade,
  created_at        timestamptz default now(),
  primary key (item_id, user_id)
);
create index if not exists mp_stars_user_idx
  on template_marketplace_stars (user_id, created_at desc);

-- ============================================================
-- RLS
-- ============================================================
alter table api_keys                  enable row level security;
alter table api_key_usage_log         enable row level security;
alter table outbound_webhooks         enable row level security;
alter table webhook_deliveries        enable row level security;
alter table template_marketplace_items enable row level security;
alter table template_marketplace_stars enable row level security;

drop policy if exists ak_select on api_keys;
drop policy if exists ak_modify on api_keys;
create policy ak_select on api_keys for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ak_modify on api_keys for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists akul_select on api_key_usage_log;
create policy akul_select on api_key_usage_log for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists ow_select on outbound_webhooks;
drop policy if exists ow_modify on outbound_webhooks;
create policy ow_select on outbound_webhooks for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy ow_modify on outbound_webhooks for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists wd_select on webhook_deliveries;
create policy wd_select on webhook_deliveries for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- Marketplace items: lectura pública para visibility='public', escritura firm-scoped
drop policy if exists mp_select_public on template_marketplace_items;
drop policy if exists mp_select_own on template_marketplace_items;
drop policy if exists mp_modify on template_marketplace_items;
create policy mp_select_public on template_marketplace_items for select
  using (visibility = 'public' or author_firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy mp_modify on template_marketplace_items for all
  using (author_firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (author_firm_id = auth_firm_id() or auth.role() = 'service_role');

drop policy if exists mps_select on template_marketplace_stars;
drop policy if exists mps_modify on template_marketplace_stars;
create policy mps_select on template_marketplace_stars for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy mps_modify on template_marketplace_stars for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- ============================================================
-- Triggers
-- ============================================================
drop trigger if exists trg_ak_firm_id on api_keys;
create trigger trg_ak_firm_id before insert on api_keys
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_akul_firm_id on api_key_usage_log;
create trigger trg_akul_firm_id before insert on api_key_usage_log
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_ow_firm_id on outbound_webhooks;
create trigger trg_ow_firm_id before insert on outbound_webhooks
  for each row execute function set_firm_id_from_jwt();
drop trigger if exists trg_ow_updated_at on outbound_webhooks;
create trigger trg_ow_updated_at before update on outbound_webhooks
  for each row execute function tg_set_updated_at();

drop trigger if exists trg_wd_firm_id on webhook_deliveries;
create trigger trg_wd_firm_id before insert on webhook_deliveries
  for each row execute function set_firm_id_from_jwt();

drop trigger if exists trg_mps_firm_id on template_marketplace_stars;
create trigger trg_mps_firm_id before insert on template_marketplace_stars
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- Seed · plantillas oficiales LexAI
-- ============================================================
insert into template_marketplace_items
  (name, doc_type, category, jurisdiction, description, body, variables, is_official)
values
  ('Tutela genérica (CO)', 'tutela', 'constitucional', 'colombia',
   'Plantilla base de acción de tutela conforme al Decreto 2591/1991.',
   E'ACCIÓN DE TUTELA\n\nSeñor Juez,\n\n{{accionante}}, identificado(a) con C.C. {{cedula}}, '
   'invocando los derechos fundamentales consagrados en los artículos {{articulos}} de la '
   'Constitución Política, interpongo ACCIÓN DE TUTELA contra {{accionado}}.\n\n'
   'HECHOS\n1. {{hechos}}\n\nPRETENSIONES\nSe ordene a la entidad accionada {{pretensiones}}.',
   array['accionante','cedula','articulos','accionado','hechos','pretensiones']::text[],
   true),
  ('Derecho de petición', 'derecho_peticion', 'administrativo', 'colombia',
   'Petición conforme al Art. 23 C.P. y Ley 1755/2015.',
   E'DERECHO DE PETICIÓN\n\nSeñor {{autoridad}},\n\n{{peticionario}}, identificado(a) con '
   '{{identificacion}}, en ejercicio del derecho fundamental de petición consagrado en el '
   'artículo 23 de la Constitución y la Ley 1755 de 2015, solicito:\n\n{{peticion_concreta}}\n\n'
   'Fundamento mi solicitud en los siguientes hechos:\n{{hechos}}',
   array['autoridad','peticionario','identificacion','peticion_concreta','hechos']::text[],
   true),
  ('Contestación de demanda laboral', 'contestacion', 'laboral', 'colombia',
   'Estructura base para contestar demanda laboral (CGP).',
   E'HONORABLE JUEZ {{juzgado}}\n\nReferencia: Proceso laboral No. {{expediente}}\n\n'
   '{{demandado}}, identificado(a) con C.C. {{cedula}}, por intermedio de mi apoderado, '
   'estando dentro de la oportunidad legal, comparezco para CONTESTAR la demanda iniciada '
   'por {{demandante}} en los siguientes términos:\n\n'
   'HECHOS\n{{respuesta_hechos}}\n\nEXCEPCIONES\n{{excepciones}}\n\n'
   'PRETENSIONES (oposición)\n{{oposicion_pretensiones}}',
   array['juzgado','expediente','demandado','cedula','demandante','respuesta_hechos','excepciones','oposicion_pretensiones']::text[],
   true),
  ('Contrato de prestación de servicios profesionales', 'contrato', 'comercial', 'colombia',
   'Modelo de contrato 1099 / prestación de servicios sin subordinación.',
   E'CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES\n\n'
   'Entre {{contratante}}, identificado(a) con NIT {{nit_contratante}}, y {{contratista}}, '
   'identificado(a) con {{id_contratista}}, se celebra el presente contrato:\n\n'
   'PRIMERA. OBJETO. {{objeto}}.\n\n'
   'SEGUNDA. DURACIÓN. Desde {{fecha_inicio}} hasta {{fecha_fin}}.\n\n'
   'TERCERA. HONORARIOS. {{honorarios}} pagaderos así: {{forma_pago}}.\n\n'
   'CUARTA. INDEPENDENCIA. Las partes declaran que entre ellas no existe vínculo laboral '
   'ni subordinación, conforme al Art. 34 CST.\n\n'
   'QUINTA. CLÁUSULA PENAL. {{clausula_penal}}.\n\n'
   'SEXTA. CONFIDENCIALIDAD. {{confidencialidad}}.\n\n'
   'En constancia se firma a los {{dias}} días del mes de {{mes}} de {{anio}}.',
   array['contratante','nit_contratante','contratista','id_contratista','objeto','fecha_inicio','fecha_fin','honorarios','forma_pago','clausula_penal','confidencialidad','dias','mes','anio']::text[],
   true)
on conflict do nothing;

-- ============================================================
-- RPC · stats
-- ============================================================
create or replace function lexai_platform_stats(p_firm_id uuid default null)
returns jsonb language sql stable as $$
  with f as (select coalesce(p_firm_id, auth_firm_id()) as id)
  select jsonb_build_object(
    'api_keys_total', (select count(*) from api_keys where firm_id = (select id from f)),
    'api_keys_active', (select count(*) from api_keys where firm_id = (select id from f) and active = true and revoked_at is null),
    'api_calls_24h', (select count(*) from api_key_usage_log where firm_id = (select id from f) and occurred_at >= now() - interval '24 hours'),
    'webhooks_total', (select count(*) from outbound_webhooks where firm_id = (select id from f)),
    'webhooks_active', (select count(*) from outbound_webhooks where firm_id = (select id from f) and active = true),
    'webhook_deliveries_24h', (select count(*) from webhook_deliveries where firm_id = (select id from f) and created_at >= now() - interval '24 hours'),
    'webhook_failures_24h', (select count(*) from webhook_deliveries where firm_id = (select id from f) and status = 'failed' and created_at >= now() - interval '24 hours'),
    'marketplace_items_official', (select count(*) from template_marketplace_items where is_official = true)
  );
$$;
