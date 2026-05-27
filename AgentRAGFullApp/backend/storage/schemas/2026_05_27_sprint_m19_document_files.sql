-- ============================================================
-- LexAI · Sprint M19.12.C1 · DOCX cache table
-- Migration date: 2026-05-27
-- Idempotent · additive
-- ============================================================
--
-- Cache de archivos generados (.docx, .pdf) por document_id.
-- Permite que el endpoint /export-forensic sirva desde DB en vez de
-- re-generar el documento en cada request, y que el archivo siga
-- disponible para descarga después del cierre de sesión.

create table if not exists document_files (
  id              uuid primary key default gen_random_uuid(),
  document_id     uuid not null,
  format          text not null check (format in ('docx', 'pdf', 'html')),
  content_bytes   bytea not null,
  size_bytes      int not null default 0,
  -- Metadata opcional
  filename        text,
  generated_by    text default 'lex_docx_builder',
  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);

-- UNIQUE (document_id, format) para soportar UPSERT
create unique index if not exists uq_document_files_doc_format
  on document_files (document_id, format);

create index if not exists idx_document_files_created
  on document_files (created_at desc);

-- Trigger updated_at
create or replace function update_document_files_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_document_files_updated_at on document_files;
create trigger trg_document_files_updated_at
  before update on document_files
  for each row execute function update_document_files_updated_at();

comment on table document_files is
  'Sprint M19.12: cache de archivos DOCX/PDF generados por documento.
   Permite descarga post-sesión y evita re-generación en cada request.';

comment on column document_files.content_bytes is
  'Bytes del archivo. Para DOCX típicamente 30-200 KB por documento legal.';
