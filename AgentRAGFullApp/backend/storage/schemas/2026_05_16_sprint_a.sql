-- ================================================================
-- Sprint A · Infraestructura común para integraciones colaborativas
-- ================================================================
-- ALCANCE: tablas, RLS, storage, pg_cron y Realtime para soportar
-- Google Drive, OneDrive, Dropbox, DocuSign sin afectar tablas
-- existentes (email_integrations / calendar_integrations /
-- whatsapp_integrations / signature_envelopes).
--
-- ADITIVO: idempotente, sin DROPs ni ALTERs destructivos.
-- ================================================================

-- ----------------------------------------------------------------
-- 1. firm_integrations · registry genérico para providers nuevos
-- ----------------------------------------------------------------
-- Convive con las 3 tablas especializadas existentes. NO las reemplaza.
-- Sólo se usa para: google_drive, onedrive, dropbox, docusign.

create table if not exists firm_integrations (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  user_id         uuid not null references users(id) on delete cascade,
  provider        text not null
    check (provider in ('google_drive','onedrive','dropbox','docusign')),
  account_id      text,                            -- ID externo (drive_user, ms_user, etc.)
  account_label   text,                            -- email o nombre legible

  -- OAuth tokens · cifrados Fernet (igual patrón sprint 7)
  oauth_access_token_enc   bytea,
  oauth_refresh_token_enc  bytea,
  oauth_expires_at         timestamptz,
  encryption_version       int default 1,
  scopes                   text[] default '{}',

  -- Para DocuSign (Camino A · MCP hosted)
  mcp_server_url           text,

  -- Estado
  status          text not null default 'pending'
    check (status in ('pending','connected','expired','revoked','error')),
  last_status     text,
  last_error      text,
  last_synced_at  timestamptz,
  active          boolean not null default true,
  metadata        jsonb default '{}'::jsonb,

  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),

  unique (firm_id, provider, account_id)
);

create index if not exists firm_integrations_firm_active_idx
  on firm_integrations (firm_id, active) where active;
create index if not exists firm_integrations_provider_status_idx
  on firm_integrations (provider, status) where active;

-- updated_at trigger
create or replace function lexai_firm_integrations_touch()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists firm_integrations_touch on firm_integrations;
create trigger firm_integrations_touch
  before update on firm_integrations
  for each row execute function lexai_firm_integrations_touch();

alter table firm_integrations enable row level security;
drop policy if exists fi_select on firm_integrations;
drop policy if exists fi_modify on firm_integrations;
create policy fi_select on firm_integrations for select
  using (firm_id = auth_firm_id());
create policy fi_modify on firm_integrations for all
  using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 2. oauth_states · PKCE + anti-CSRF · TTL 10 min
-- ----------------------------------------------------------------
-- Reemplaza el patrón inseguro de "state = integration_id" que
-- usaban email_integrations.py y calendar_integrations.py.
-- (refactor compat-safe en Sprint A.3)

create table if not exists oauth_states (
  state           text primary key,                -- nonce aleatorio 32+ bytes
  firm_id         uuid not null,
  user_id         uuid not null,
  provider        text not null,                   -- google_drive | onedrive | dropbox | docusign | google | outlook | gmail
  code_verifier   text,                            -- PKCE verifier (S256)
  redirect_to     text,                            -- frontend post-callback URL
  metadata        jsonb default '{}'::jsonb,
  expires_at      timestamptz not null default (now() + interval '10 minutes'),
  created_at      timestamptz default now()
);
-- Sin RLS · sólo accedida por service_role (Edge Function + backend)

create index if not exists oauth_states_expires_idx
  on oauth_states (expires_at);

-- ----------------------------------------------------------------
-- 3. cloud_folder_watchers · qué carpetas auto-ingestar
-- ----------------------------------------------------------------
-- Una row por cada carpeta vigilada (1 carpeta puede vincularse
-- a un matter para auto-asociar archivos).

create table if not exists cloud_folder_watchers (
  id              uuid primary key default gen_random_uuid(),
  firm_id         uuid not null references firms(id) on delete cascade,
  integration_id  uuid not null references firm_integrations(id) on delete cascade,
  provider        text not null
    check (provider in ('google_drive','onedrive','dropbox')),
  cloud_folder_id text not null,                   -- folder ID en la nube
  folder_path     text,                            -- path legible (opcional)
  matter_id       uuid references matters(id) on delete set null,
  auto_match_by_name  boolean default true,
  last_sync_cursor    text,                        -- syncToken (Drive), deltaLink (Graph), cursor (Dropbox)
  last_synced_at      timestamptz,
  active          boolean default true,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),
  unique (integration_id, cloud_folder_id)
);

create index if not exists cloud_folder_watchers_firm_active_idx
  on cloud_folder_watchers (firm_id, active) where active;
create index if not exists cloud_folder_watchers_integration_idx
  on cloud_folder_watchers (integration_id) where active;

drop trigger if exists cloud_folder_watchers_touch on cloud_folder_watchers;
create trigger cloud_folder_watchers_touch
  before update on cloud_folder_watchers
  for each row execute function lexai_firm_integrations_touch();

alter table cloud_folder_watchers enable row level security;
drop policy if exists cfw_select on cloud_folder_watchers;
drop policy if exists cfw_modify on cloud_folder_watchers;
create policy cfw_select on cloud_folder_watchers for select
  using (firm_id = auth_firm_id());
create policy cfw_modify on cloud_folder_watchers for all
  using (firm_id = auth_firm_id());

-- ----------------------------------------------------------------
-- 4. Storage bucket 'documents' · RLS por firm_id
-- ----------------------------------------------------------------
-- Estructura de paths: documents/{firm_id}/{matter_id}/{filename}
-- RLS garantiza que sólo miembros de la firm vean sus archivos.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'documents',
  'documents',
  false,                                           -- privado · acceso vía signed URLs
  104857600,                                       -- 100 MB por archivo
  array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel',
    'text/plain',
    'text/csv',
    'image/png',
    'image/jpeg',
    'image/webp'
  ]
)
on conflict (id) do update set
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- Policies storage.objects · sólo miembros de la firm pueden leer/escribir
-- (first folder segment es el firm_id)
drop policy if exists "lexai_documents_select" on storage.objects;
drop policy if exists "lexai_documents_insert" on storage.objects;
drop policy if exists "lexai_documents_update" on storage.objects;
drop policy if exists "lexai_documents_delete" on storage.objects;

create policy "lexai_documents_select"
  on storage.objects for select
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = auth_firm_id()::text
  );

create policy "lexai_documents_insert"
  on storage.objects for insert
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = auth_firm_id()::text
  );

create policy "lexai_documents_update"
  on storage.objects for update
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = auth_firm_id()::text
  );

create policy "lexai_documents_delete"
  on storage.objects for delete
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = auth_firm_id()::text
  );

-- ----------------------------------------------------------------
-- 5. pg_cron · purga oauth_states expirados cada 5 min
-- ----------------------------------------------------------------
-- Si el job ya existe (rerun de migración), unschedule primero.
do $$
declare
  v_jobid bigint;
begin
  select jobid into v_jobid from cron.job where jobname = 'oauth_states_purge';
  if v_jobid is not null then
    perform cron.unschedule(v_jobid);
  end if;
end $$;

select cron.schedule(
  'oauth_states_purge',
  '*/5 * * * *',
  $$ delete from public.oauth_states where expires_at < now() $$
);

-- ----------------------------------------------------------------
-- 6. Realtime publication · live updates en frontend
-- ----------------------------------------------------------------
-- Habilita supabase.channel().on('postgres_changes', ...) en cliente.

do $$
begin
  -- firm_integrations
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'firm_integrations'
  ) then
    alter publication supabase_realtime add table firm_integrations;
  end if;

  -- cloud_folder_watchers
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'cloud_folder_watchers'
  ) then
    alter publication supabase_realtime add table cloud_folder_watchers;
  end if;
end $$;

-- ----------------------------------------------------------------
-- 7. Verificación (queries de sanidad · output al ejecutar)
-- ----------------------------------------------------------------
select
  'firm_integrations'  as tabla,
  count(*) as rows,
  (select count(*) from pg_policies where tablename = 'firm_integrations') as policies
from firm_integrations
union all
select 'oauth_states', count(*),
  (select count(*) from pg_policies where tablename = 'oauth_states')
from oauth_states
union all
select 'cloud_folder_watchers', count(*),
  (select count(*) from pg_policies where tablename = 'cloud_folder_watchers')
from cloud_folder_watchers
union all
select 'pg_cron job',
  (select count(*) from cron.job where jobname = 'oauth_states_purge'),
  null
union all
select 'realtime publication firm_integrations',
  (select count(*) from pg_publication_tables
    where pubname = 'supabase_realtime' and tablename = 'firm_integrations'),
  null
union all
select 'storage bucket documents',
  (select count(*) from storage.buckets where id = 'documents'),
  null;
