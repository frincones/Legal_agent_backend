-- ============================================================
-- LexAI · F1 · Análisis profundo IA de documentos
-- Migration date: 2026-05-03
-- Idempotent · additive · NO DROP
-- ============================================================

-- 1. document_extractions · una fila por (matter_document_id × version)
create table if not exists document_extractions (
  id                    uuid primary key default gen_random_uuid(),
  firm_id               uuid not null references firms(id) on delete cascade,
  matter_id             uuid references matters(id) on delete cascade,
  matter_document_id    uuid not null references matter_documents(id) on delete cascade,
  status                text not null default 'pending'  -- 'pending'|'processing'|'completed'|'failed'
    check (status in ('pending','processing','completed','failed')),
  parties_jsonb         jsonb not null default '[]'::jsonb,    -- [{rol, nombre, tax_id, personal_id, confianza}]
  dates_jsonb           jsonb not null default '[]'::jsonb,    -- [{tipo, fecha, descripcion, confianza}]
  obligations_jsonb     jsonb not null default '[]'::jsonb,    -- [{deudor, acreedor, descripcion, monto_cop, vencimiento, confianza}]
  montos_jsonb          jsonb not null default '[]'::jsonb,    -- [{concepto, monto, moneda, confianza}]
  inconsistencies_jsonb jsonb not null default '[]'::jsonb,    -- [{descripcion, severidad}]
  hechos_clave          text,                                  -- resumen narrativo (opcional)
  riesgos_legales       jsonb not null default '[]'::jsonb,    -- [{riesgo, severidad, fundamento}]
  vacios_probatorios    jsonb not null default '[]'::jsonb,    -- [{descripcion, sugerencia}]
  confidence_score      real,                                  -- 0..1
  model_used            text,                                  -- 'gpt-4o-mini'
  prompt_version        text default 'extract-v1',
  pages_processed       int,
  prev_extraction_id    uuid references document_extractions(id),
  error_message         text,
  extracted_at          timestamptz default now()
);
create index if not exists doc_ext_firm_idx
  on document_extractions (firm_id, extracted_at desc);
create index if not exists doc_ext_doc_idx
  on document_extractions (matter_document_id, extracted_at desc);
create index if not exists doc_ext_matter_idx
  on document_extractions (matter_id) where matter_id is not null;
create index if not exists doc_ext_status_idx
  on document_extractions (firm_id, status) where status in ('pending','processing');

-- RLS estándar
alter table document_extractions enable row level security;

drop policy if exists document_extractions_select on document_extractions;
drop policy if exists document_extractions_modify on document_extractions;
create policy document_extractions_select on document_extractions for select
  using (firm_id = auth_firm_id() or auth.role() = 'service_role');
create policy document_extractions_modify on document_extractions for all
  using (firm_id = auth_firm_id() or auth.role() = 'service_role')
  with check (firm_id = auth_firm_id() or auth.role() = 'service_role');

-- Trigger firm_id auto
drop trigger if exists trg_document_extractions_firm_id on document_extractions;
create trigger trg_document_extractions_firm_id
  before insert on document_extractions
  for each row execute function set_firm_id_from_jwt();

-- ============================================================
-- 2. matter_parties: añadir column origen para distinguir IA-extraídas vs manuales
-- ============================================================
alter table matter_parties
  add column if not exists origen text default 'manual';
-- 'manual' | 'ai_extracted' | 'imported'

-- ============================================================
-- 3. RPC · resumen del estado de extracciones por matter
-- ============================================================
create or replace function lexai_extract_status(p_matter_id uuid)
returns jsonb
language sql
stable
as $$
  with last_ext as (
    select
      matter_document_id,
      status,
      confidence_score,
      jsonb_array_length(parties_jsonb) as n_parties,
      jsonb_array_length(obligations_jsonb) as n_oblig,
      jsonb_array_length(inconsistencies_jsonb) as n_incons,
      extracted_at,
      row_number() over (partition by matter_document_id order by extracted_at desc) as rn
    from document_extractions
    where matter_id = p_matter_id
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'matter_document_id', matter_document_id,
    'status', status,
    'confidence', confidence_score,
    'n_parties', n_parties,
    'n_obligations', n_oblig,
    'n_inconsistencies', n_incons,
    'extracted_at', extracted_at
  )), '[]'::jsonb)
  from last_ext
  where rn = 1;
$$;
