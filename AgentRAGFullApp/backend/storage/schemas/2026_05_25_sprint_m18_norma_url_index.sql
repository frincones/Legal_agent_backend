-- ============================================================
-- LexAI · Sprint M18 · Norma URL Index (generalizable URL discovery)
-- Migration date: 2026-05-25
-- Idempotent · additive · backward-compatible (no breaking changes)
-- ============================================================
--
-- Tabla GLOBAL (sin RLS): URLs canónicas validadas a fuentes oficiales
-- colombianas. Poblada por:
--   1. Seed inicial (scripts/seed_norma_url_index.py): 24 hardcoded entries
--   2. Lazy discovery on-demand (SmartSearchTool → Brave Search API)
--   3. Tools live fetch (Corte CC, CSJ RSS, etc.) que descubran URL real
--
-- Provenance trackeada en `discovered_by` para auditoría legal.

create table if not exists norma_url_index (
  id                   uuid primary key default gen_random_uuid(),

  -- Lookup key (matches ParsedCitation de utils.citation_verifier)
  kind                 text not null,
  tipo                 text not null,
  numero               int,
  anio                 int,
  normalized_ref       text not null,

  -- URL descubierta y validada
  fuente_url           text not null,
  url_validated        boolean not null default false,
  url_http_status      int,
  body_size_bytes      int,

  -- Metadata enriquecida
  titulo               text,
  snippet              text,
  vigencia             text,

  -- Provenance (CRITICAL para auditoría)
  discovered_by        text not null,
  query_used           text,

  -- Lifecycle
  confidence           numeric(4,3) default 0.0,
  last_validated_at    timestamptz default now(),
  revalidate_after     timestamptz default (now() + interval '7 days'),
  validation_failures  int default 0,
  hit_count            int default 0,
  last_hit_at          timestamptz,

  created_at           timestamptz default now(),
  updated_at           timestamptz default now()
);

-- Unique constraints: una norma única → una entrada (upsert)
-- Para normas con numero+anio (ley, decreto, jurisprudencia)
create unique index if not exists uq_norma_url_index_full
  on norma_url_index (kind, tipo, numero, anio)
  where numero is not null and anio is not null;

-- Para códigos sin numero/anio (codigo)
create unique index if not exists uq_norma_url_index_codigo
  on norma_url_index (kind, tipo)
  where numero is null and anio is null;

-- Para codigo_articulo (numero del artículo, sin anio)
create unique index if not exists uq_norma_url_index_codigo_art
  on norma_url_index (kind, tipo, numero)
  where numero is not null and anio is null;

-- Lookup por normalized_ref (debug)
create index if not exists idx_norma_url_index_normalized
  on norma_url_index (normalized_ref);

-- Para revalidation worker (M19)
create index if not exists idx_norma_url_index_revalidate
  on norma_url_index (revalidate_after)
  where url_validated = true;

-- Para métricas
create index if not exists idx_norma_url_index_discovered_by
  on norma_url_index (discovered_by);

-- Trigger updated_at
create or replace function update_norma_url_index_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_norma_url_index_updated_at on norma_url_index;
create trigger trg_norma_url_index_updated_at
  before update on norma_url_index
  for each row execute function update_norma_url_index_updated_at();

-- Comments para documentación inline
comment on table norma_url_index is
  'Sprint M18: Índice global de URLs canónicas para normas colombianas.
   Poblado por SmartSearchTool (Brave) + seed manual + lazy discovery.
   Tabla compartida entre firms (URLs gov.co son públicas).';

comment on column norma_url_index.discovered_by is
  'Origen: pattern|brave_search|internal_db|live_fetch|manual|llm_fallback';

comment on column norma_url_index.snippet is
  'Texto extraído por el search engine que confirma la cita.
   Se muestra al usuario como evidencia.';

comment on column norma_url_index.revalidate_after is
  'Timestamp tras el cual el RevalidationWorker (M19) debe re-validar.
   Default +7d. Para derogadas: +90d.';
