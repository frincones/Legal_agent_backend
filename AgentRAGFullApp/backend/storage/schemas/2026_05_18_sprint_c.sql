-- ================================================================
-- Sprint C · Documentos · Drive + OneDrive + Dropbox
-- ================================================================
-- Sync de carpetas vigiladas → matter_documents + RAG indexing.
-- Reusa cloud_folder_watchers (creada en sprint A).
--
-- ADITIVO: solo añade columnas + Realtime + pg_cron.
-- NO toca contratos existentes.
-- ================================================================

-- ----------------------------------------------------------------
-- 1. matter_documents · origen de la nube
-- ----------------------------------------------------------------

alter table matter_documents
  add column if not exists source_provider text
    check (source_provider is null or
           source_provider in ('manual', 'google_drive', 'onedrive', 'dropbox', 'canvas', 'docusign'));

alter table matter_documents
  add column if not exists source_external_id text;

alter table matter_documents
  add column if not exists source_integration_id uuid references firm_integrations(id) on delete set null;

alter table matter_documents
  add column if not exists source_watcher_id uuid references cloud_folder_watchers(id) on delete set null;

alter table matter_documents
  add column if not exists source_url text;        -- URL al archivo original en la nube

alter table matter_documents
  add column if not exists last_synced_at timestamptz;

-- Unique para idempotencia · evitar duplicados al re-sincronizar
create unique index if not exists matter_documents_source_unique_idx
  on matter_documents (source_integration_id, source_external_id)
  where source_integration_id is not null and source_external_id is not null;

create index if not exists matter_documents_provider_idx
  on matter_documents (firm_id, source_provider)
  where source_provider is not null;

-- ----------------------------------------------------------------
-- 2. Realtime publication para matter_documents (live UI)
-- ----------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'matter_documents'
  ) then
    alter publication supabase_realtime add table matter_documents;
  end if;
end $$;

-- ----------------------------------------------------------------
-- 3. pg_cron · cloud_delta_sync cada 30min
-- ----------------------------------------------------------------

do $$
declare
  v_jobid bigint;
  v_railway_url text := 'https://legal-agent-backend-production-fcfa.up.railway.app';
  v_cron_secret text;
begin
  begin
    v_cron_secret := current_setting('app.cron_secret', true);
  exception when others then
    v_cron_secret := null;
  end;
  if v_cron_secret is null or v_cron_secret = '' then
    v_cron_secret := 'NOT_SET';
  end if;

  select jobid into v_jobid from cron.job where jobname = 'cloud_delta_sync';
  if v_jobid is not null then
    perform cron.unschedule(v_jobid);
  end if;

  perform cron.schedule(
    'cloud_delta_sync',
    '*/30 * * * *',
    format(
      $cron$
      select net.http_post(
        url := %L,
        headers := jsonb_build_object(
          'Content-Type', 'application/json',
          'X-Cron-Secret', %L
        ),
        body := '{"type":"cloud"}'::jsonb
      )
      $cron$,
      v_railway_url || '/v1/admin/sync-tick?type=cloud',
      v_cron_secret
    )
  );
end $$;

-- ----------------------------------------------------------------
-- 4. RPC helper · listar docs de un caso con origen
-- ----------------------------------------------------------------

create or replace function lexai_matter_documents_enriched(p_matter_id uuid)
returns table (
  id uuid,
  titulo text,
  kind text,
  status text,
  byte_size bigint,
  pages int,
  mime_type text,
  storage_path text,
  source_provider text,
  source_url text,
  last_synced_at timestamptz,
  created_at timestamptz
)
language sql stable security definer as $$
  select d.id, d.titulo, d.kind::text, d.status::text,
         d.byte_size, d.pages, d.mime_type, d.storage_path,
         d.source_provider, d.source_url, d.last_synced_at, d.created_at
    from matter_documents d
   where d.matter_id = p_matter_id
   order by d.created_at desc
   limit 200;
$$;

grant execute on function lexai_matter_documents_enriched(uuid) to authenticated, service_role;

-- ----------------------------------------------------------------
-- 5. Verificación
-- ----------------------------------------------------------------

select
  'matter_documents new cols' as check,
  count(*) filter (where column_name in ('source_provider','source_external_id',
                                          'source_integration_id','source_watcher_id',
                                          'source_url','last_synced_at')) as cols_added
  from information_schema.columns
 where table_schema = 'public' and table_name = 'matter_documents'
union all
select 'matter_documents source unique idx',
  (select count(*) from pg_indexes
    where tablename='matter_documents' and indexname='matter_documents_source_unique_idx')
union all
select 'realtime matter_documents publication',
  (select count(*) from pg_publication_tables
    where pubname='supabase_realtime' and tablename='matter_documents')
union all
select 'pg_cron cloud_delta_sync',
  (select count(*) from cron.job where jobname='cloud_delta_sync')
union all
select 'lexai_matter_documents_enriched fn',
  (select count(*) from pg_proc where proname='lexai_matter_documents_enriched');
