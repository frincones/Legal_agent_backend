-- ============================================================
-- LexAI · Sprint M0 · Block streaming + Audit + Templates v2
-- Migration date: 2026-05-26
-- Idempotent · additive · backward-compatible (no breaking changes)
-- ============================================================
--
-- Crea infraestructura para Sprint M (Document Generation v3.1):
--   1. document_blocks       — bloques tipados serializados por document_id
--   2. generation_audit      — auditoría persistida (compliance 5 años)
--   3. template_catalog      — TemplateDef versionado (12 templates iniciales)
--   4. citation_verifications — cita → chunk_id + estado verificado
--   5. document_versions     — historial de versiones con diff
--   6. ALTER external_fetch_cache — añade last_hit_at + auto_ingested_at
-- ============================================================

-- ------------------------------------------------------------
-- 1. document_blocks
-- ------------------------------------------------------------
-- Cada documento generado se serializa como secuencia de bloques tipados.
-- Permite WYSIWYG canvas ↔ .docx export y regeneración por sección.
create table if not exists document_blocks (
  id              uuid primary key default gen_random_uuid(),
  document_id     uuid not null,                  -- soft FK a matter_documents.id
  generation_id   uuid,                            -- soft FK a generation_audit
  section_key     text not null,                   -- 'partes', 'hechos', etc.
  block_order     int not null,                    -- orden dentro del documento (no de sección)
  block_id        text not null unique,            -- id externo (uuid string) referenciado por SSE
  block_type      text not null,                   -- 'title' | 'paragraph' | 'hecho' | ...
  block_data      jsonb not null,                  -- payload completo del bloque
  created_at      timestamptz not null default now()
);

create index if not exists document_blocks_doc_order_idx
  on document_blocks (document_id, block_order);
create index if not exists document_blocks_section_idx
  on document_blocks (document_id, section_key);
create index if not exists document_blocks_block_id_idx
  on document_blocks (block_id);

alter table document_blocks enable row level security;

drop policy if exists document_blocks_select on document_blocks;
create policy document_blocks_select on document_blocks for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists document_blocks_modify on document_blocks;
create policy document_blocks_modify on document_blocks for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ------------------------------------------------------------
-- 2. generation_audit
-- ------------------------------------------------------------
-- Audit trail completo de cada generación de documento (compliance jurídica).
-- citations / calculations / warnings se almacenan denormalizado en jsonb
-- para retrieval rápido sin joins.
create table if not exists generation_audit (
  id                 uuid primary key default gen_random_uuid(),
  generation_id      uuid not null unique,         -- emitido al frontend en SSE
  firm_id            uuid,                          -- soft FK a firms (multi-tenant)
  matter_id          uuid,                          -- soft FK a matters
  document_id        uuid,                          -- soft FK a matter_documents
  template_id        text not null,                 -- 'demanda_laboral_ordinaria' | ...
  template_version   int,
  model_used         jsonb not null default '{}'::jsonb,  -- { classifier, generator, polish, qa }
  duration_seconds   numeric(8,2) not null default 0,
  cost_usd           numeric(10,5) not null default 0,
  citations          jsonb not null default '[]'::jsonb,
  calculations       jsonb not null default '{}'::jsonb,
  validation_passed  boolean not null default false,
  qa_score           numeric(4,2),                  -- 0.00-10.00
  warnings           jsonb not null default '[]'::jsonb,
  audit_json         jsonb not null default '{}'::jsonb,  -- snapshot completo
  created_at         timestamptz not null default now()
);

create index if not exists generation_audit_firm_created_idx
  on generation_audit (firm_id, created_at desc);
create index if not exists generation_audit_generation_id_idx
  on generation_audit (generation_id);
create index if not exists generation_audit_document_id_idx
  on generation_audit (document_id);

alter table generation_audit enable row level security;

drop policy if exists generation_audit_select on generation_audit;
create policy generation_audit_select on generation_audit for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists generation_audit_modify on generation_audit;
create policy generation_audit_modify on generation_audit for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ------------------------------------------------------------
-- 3. template_catalog
-- ------------------------------------------------------------
-- TemplateDef versionado. La definicion Python sigue siendo source-of-truth,
-- pero esto persiste el snapshot para auditoría (qué template generó qué doc).
create table if not exists template_catalog (
  id              text not null,                   -- 'demanda_laboral_ordinaria'
  version         int not null default 1,
  jurisdiccion    text,                            -- 'laboral' | 'civil' | 'penal' | 'constitucional' | 'administrativo' | 'familia' | 'tributario' | null
  nombre          text not null,
  forensic_style  text not null default 'demanda', -- 'demanda' | 'tutela' | 'contrato' | 'denuncia' | 'recurso' | 'concepto' | 'poder'
  definition      jsonb not null,                  -- TemplateDef serializado
  is_active       boolean not null default true,
  created_at      timestamptz not null default now(),
  primary key (id, version)
);

create index if not exists template_catalog_jurisdiccion_idx
  on template_catalog (jurisdiccion, is_active);
create index if not exists template_catalog_active_idx
  on template_catalog (is_active);

alter table template_catalog enable row level security;

drop policy if exists template_catalog_select on template_catalog;
create policy template_catalog_select on template_catalog for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists template_catalog_modify on template_catalog;
create policy template_catalog_modify on template_catalog for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ------------------------------------------------------------
-- 4. citation_verifications
-- ------------------------------------------------------------
-- Cada cita (norma o jurisprudencia) emitida durante generación queda
-- registrada con su estado de verificación (chunk match, vigencia).
create table if not exists citation_verifications (
  id                  uuid primary key default gen_random_uuid(),
  generation_id       uuid not null,
  document_id         uuid,
  block_id            text,                         -- bloque donde aparece la cita
  citation_text       text not null,                -- 'Art. 64 CST' | 'SL1430-2022'
  citation_type       text not null,                -- 'norma' | 'jurisprudencia'
  chunk_id            uuid,                         -- soft FK a chunks.id si verificada
  source_url          text,                         -- URL externa si vino de hot-fetch
  similarity_score    numeric(4,3),                 -- 0.000-1.000
  verified            boolean not null default false,
  derogada            boolean,                      -- null si norma no aplica check
  verification_method text,                         -- 'rag' | 'external_fetch' | 'manual' | 'fallback'
  verified_at         timestamptz not null default now()
);

create index if not exists citation_verifications_gen_idx
  on citation_verifications (generation_id);
create index if not exists citation_verifications_doc_idx
  on citation_verifications (document_id);
create index if not exists citation_verifications_text_idx
  on citation_verifications (citation_text, citation_type);

alter table citation_verifications enable row level security;

drop policy if exists citation_verifications_select on citation_verifications;
create policy citation_verifications_select on citation_verifications for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists citation_verifications_modify on citation_verifications;
create policy citation_verifications_modify on citation_verifications for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ------------------------------------------------------------
-- 5. document_versions
-- ------------------------------------------------------------
-- Historial de versiones de un documento. Cada regeneración de sección o
-- edición manual crea una versión nueva con el diff respecto a la previa.
create table if not exists document_versions (
  id                  uuid primary key default gen_random_uuid(),
  document_id         uuid not null,                -- soft FK a matter_documents
  version_num         int not null,                  -- 1, 2, 3, ...
  parent_version_id   uuid,                          -- version anterior (null para v1)
  change_type         text not null,                 -- 'initial' | 'regenerate_section' | 'manual_edit' | 'polish' | 'rebuild'
  section_key         text,                          -- si change_type es 'regenerate_section'
  blocks_snapshot     jsonb not null,                -- snapshot de TODOS los bloques
  blocks_diff         jsonb,                          -- diff vs parent (null en v1)
  feedback            text,                          -- prompt del usuario al regenerar
  created_by          uuid,                          -- soft FK a users
  created_at          timestamptz not null default now(),
  unique (document_id, version_num)
);

create index if not exists document_versions_doc_idx
  on document_versions (document_id, version_num desc);

alter table document_versions enable row level security;

drop policy if exists document_versions_select on document_versions;
create policy document_versions_select on document_versions for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists document_versions_modify on document_versions;
create policy document_versions_modify on document_versions for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');


-- ------------------------------------------------------------
-- 6. ALTER external_fetch_cache — añadir métricas para auto-ingest
-- ------------------------------------------------------------
-- hit_count ya existe (Sprint F2). Añadimos last_hit_at + auto_ingested_at
-- para que el worker auto-ingest sepa qué promover al core corpus.
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'external_fetch_cache' and column_name = 'last_hit_at'
  ) then
    alter table external_fetch_cache add column last_hit_at timestamptz;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'external_fetch_cache' and column_name = 'auto_ingested_at'
  ) then
    alter table external_fetch_cache add column auto_ingested_at timestamptz;
  end if;
end $$;

create index if not exists external_fetch_cache_hits_for_ingest_idx
  on external_fetch_cache (hit_count desc, auto_ingested_at)
  where auto_ingested_at is null;


-- ------------------------------------------------------------
-- 7. View v_hunter_health — dashboard de salud de hunters/RAG/hot-fetch
-- ------------------------------------------------------------
-- Solo crear si verification_attempts existe (sprint L). Si no, es no-op.
do $$
begin
  if exists (
    select 1 from information_schema.tables
    where table_name = 'verification_attempts'
  ) then
    execute $sql$
      create or replace view v_hunter_health as
      select
        source,
        result_state,
        count(*) as total,
        round(avg(coalesce(duration_ms, 0))::numeric, 0) as avg_ms,
        round(100.0 * count(*) filter (where result_state = 'no_encontrada')
              / nullif(count(*), 0), 2) as miss_rate_pct,
        max(created_at) as last_attempt_at
      from verification_attempts
      where created_at > now() - interval '7 days'
      group by source, result_state
      order by miss_rate_pct desc nulls last
    $sql$;
  end if;
end $$;


-- ============================================================
-- FIN Sprint M0
-- ============================================================
