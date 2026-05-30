-- ============================================================
-- LexAI · Sprint M20.13 · derogation_inference_cache
-- Migration date: 2026-05-29
-- Idempotente · additive · no breaking changes
-- ============================================================

create table if not exists derogation_inference_cache (
  id              uuid primary key default gen_random_uuid(),
  text_hash       text not null unique,           -- sha256 del norma_text (32 chars)
  norma_text      text not null,
  tier            text not null check (
    tier in ('GROUNDED', 'DEROGADA', 'MODULADA', 'VERIFY_FLAG', 'NOT_FOUND')
  ),
  confidence      numeric(3,2) not null default 0.50,
  derogada_por    text,
  modulada_por    text,
  explanation     text,
  detection_method text not null check (
    detection_method in ('heuristic', 'cache', 'tabla', 'llm')
  ),
  created_at      timestamptz not null default now(),
  expires_at      timestamptz                       -- null = no expira
);

create index if not exists derogation_cache_hash_idx
  on derogation_inference_cache (text_hash);

create index if not exists derogation_cache_expires_idx
  on derogation_inference_cache (expires_at);

create index if not exists derogation_cache_tier_idx
  on derogation_inference_cache (tier, created_at desc);

alter table derogation_inference_cache enable row level security;

drop policy if exists dic_select on derogation_inference_cache;
create policy dic_select on derogation_inference_cache for select
  using (auth.role() in ('authenticated', 'service_role'));

drop policy if exists dic_modify on derogation_inference_cache;
create policy dic_modify on derogation_inference_cache for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table derogation_inference_cache is
  'M20.13: cache de detecciones de derogación tácita / modulación constitucional.
   Permite evitar re-llamar LLM por consulta repetida. text_hash es sha256
   del norma_text normalizado.';
