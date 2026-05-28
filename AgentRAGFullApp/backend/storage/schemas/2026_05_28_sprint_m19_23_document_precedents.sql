-- ============================================================
-- LexAI · Sprint M19.23.A.2 · Document Precedents (RAG opcional)
-- Migration date: 2026-05-28
-- Idempotent · additive · no breaking changes
-- ============================================================
--
-- Tabla opcional (fase 2) para RAG de documentos previos exitosos.
-- El structure_discovery podrá consultar precedentes con embeddings
-- vectoriales para mejorar el plan generado: "documentos similares
-- exitosos usaron esta estructura". Se enriquece automáticamente
-- cuando el abogado aprueba un documento (approval_score >= 4).
--
-- En la fase 1 (M19.23 inicial) esta tabla queda vacía. Cuando se
-- active el feature `precedents_rag_enabled=true`, los nuevos docs
-- aprobados se ingestan automáticamente.

create table if not exists document_precedents (
  id                uuid primary key default gen_random_uuid(),
  document_id       uuid,                          -- FK lógica a matter_documents (sin constraint dura por compat)
  structure_key     text not null,                 -- misma key que structure_recipes
  doc_type          text not null,
  jurisdiccion      text,

  -- Snapshot del documento aprobado (bloques completos)
  blocks_snapshot   jsonb not null,

  -- Embedding del contenido completo para búsqueda semántica
  -- (la columna existe pero pgvector solo se activa si está disponible)
  embedding         vector(1536),                  -- opcional, requiere extensión pgvector

  -- Aprobación del abogado
  approved_by       uuid,                          -- user_id del abogado
  approval_score    int check (approval_score between 1 and 5),
  approval_notes    text,

  -- Metadata
  firm_id           uuid,                          -- multi-tenancy
  matter_id         uuid,
  intent_summary    text,                          -- intent original del usuario
  generation_id     uuid,                          -- vincula con generation_audit

  created_at        timestamptz default now()
);

create index if not exists idx_precedents_structure_key
  on document_precedents (structure_key);

create index if not exists idx_precedents_doctype
  on document_precedents (doc_type);

create index if not exists idx_precedents_firm
  on document_precedents (firm_id);

-- Índice vectorial — solo se crea si pgvector está disponible
-- (se intenta crear de forma segura, ignora si la extensión falta)
do $$
begin
  if exists (select 1 from pg_extension where extname = 'vector') then
    -- Solo crear si no existe ya
    if not exists (select 1 from pg_indexes where indexname = 'idx_precedents_embedding') then
      create index idx_precedents_embedding on document_precedents
        using ivfflat (embedding vector_cosine_ops) with (lists = 100);
    end if;
  end if;
exception when others then
  -- Ignorar errores: el índice vectorial es opcional
  null;
end $$;

comment on table document_precedents is
  'Sprint M19.23.A.2: RAG opcional de docs previos exitosos para enriquecer
   el structure_discovery. Se llena automáticamente cuando un abogado
   aprueba un documento (approval_score >= 4). En fase 1 queda vacía.';
