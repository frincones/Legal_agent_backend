-- ================================================================
-- Sprint D · DocuSign · firma electrónica desde Canvas
-- ================================================================
-- ADITIVO: solo Realtime publication + RPC helper.
-- signature_envelopes ya existe (sprint 13) y firm_integrations
-- ya soporta provider='docusign' (sprint A).
-- ================================================================

-- ----------------------------------------------------------------
-- 1. Realtime publication para envelopes (estado live en /casos/[id]/firmas)
-- ----------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_publication_tables
     where pubname = 'supabase_realtime'
       and schemaname = 'public'
       and tablename = 'signature_envelopes'
  ) then
    alter publication supabase_realtime add table signature_envelopes;
  end if;
end $$;

-- ----------------------------------------------------------------
-- 2. Columnas adicionales para metadata signers (si no existen)
-- ----------------------------------------------------------------

alter table signature_envelopes
  add column if not exists signers jsonb default '[]'::jsonb;

alter table signature_envelopes
  add column if not exists source_document_id uuid references matter_documents(id) on delete set null;

alter table signature_envelopes
  add column if not exists archived_to_cloud boolean default false;

-- ----------------------------------------------------------------
-- 3. RPC helper · envelopes del caso con datos enriquecidos
-- ----------------------------------------------------------------

create or replace function lexai_matter_envelopes(p_matter_id uuid)
returns table (
  id uuid,
  title text,
  provider text,
  external_id text,
  status text,
  signers jsonb,
  signer_count int,
  signed_count int,
  sent_at timestamptz,
  completed_at timestamptz,
  expires_at timestamptz,
  signed_pdf_storage_path text,
  created_at timestamptz
)
language sql stable security definer as $$
  select id, title, provider, external_id, status, signers,
         signer_count, signed_count, sent_at, completed_at,
         expires_at, signed_pdf_storage_path, created_at
    from signature_envelopes
   where matter_id = p_matter_id
   order by created_at desc
   limit 100;
$$;

grant execute on function lexai_matter_envelopes(uuid) to authenticated, service_role;

-- ----------------------------------------------------------------
-- 4. Verificación
-- ----------------------------------------------------------------

select
  'realtime signature_envelopes publication' as check,
  (select count(*) from pg_publication_tables
    where pubname='supabase_realtime' and tablename='signature_envelopes') as count
union all
select 'signature_envelopes new cols',
  (select count(*) from information_schema.columns
    where table_name='signature_envelopes'
      and column_name in ('signers','source_document_id','archived_to_cloud'))
union all
select 'lexai_matter_envelopes fn',
  (select count(*) from pg_proc where proname='lexai_matter_envelopes');
