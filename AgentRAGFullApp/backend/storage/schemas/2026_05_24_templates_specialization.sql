-- ============================================================
-- 2026-05-24 · Templates Specialization + Multi-Agent Audit
-- ============================================================
-- Purpose: Extend the existing user_templates infrastructure (Sprint 4,
--          2026_05_10_sprint4.sql) with metadata required by the new
--          multi-agent document generator (Sprint 3) and quality
--          validator (Sprint 4). Also adds two helper tables:
--
--          - template_checklists      · YAML rules per (materia × kind)
--          - template_candidates      · staging area for scraped templates
--                                       awaiting curator review
--          - template_generations     · audit trail for multi-agent runs
--                                       (complements skill_executions for
--                                       multi-step LangGraph orchestrations)
--
-- SAFETY:
--   - 100% additive · NO DROP, NO column rename, NO data loss
--   - All ALTER statements use ADD COLUMN IF NOT EXISTS
--   - All CREATE TABLE / INDEX / POLICY use IF NOT EXISTS
--   - RLS policies are NEW (different names from existing ones), so they
--     coexist with current policies rather than replacing them
--   - The single non-additive change is dropping the NOT NULL on
--     user_templates.firm_id · this is REQUIRED because legal_templates_api.py
--     already queries `firm_id IS NULL` (a pre-existing API/schema mismatch)
--
-- ROLLBACK plan if needed (manual, by operator):
--   alter table user_templates drop column if exists materia;
--   alter table user_templates drop column if exists subtype;
--   ...etc
--   drop table if exists template_checklists;
--   drop table if exists template_candidates;
--   drop table if exists template_generations;
--   alter table user_templates alter column firm_id set not null;  (only if
--     no rows have NULL firm_id at rollback time)
--
-- HOW TO APPLY:
--   The Supabase service-role connection is the only safe path. The pattern
--   used by sibling migrations is to apply via the project README workflow
--   (see storage/schemas/README_LEXAI.md). DO NOT auto-apply from CI without
--   a manual review of an EXPLAIN ANALYZE on staging first.
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. user_templates · loosen firm_id + add specialization columns
-- ──────────────────────────────────────────────────────────────

-- 1.a · Allow firm_id IS NULL for SYSTEM (product-wide) templates.
--       legal_templates_api.py:_list_db_templates already expects this;
--       this aligns the schema with the API behavior.
alter table user_templates
  alter column firm_id drop not null;

-- 1.b · Materia (uses existing materia_legal enum: 11 values incl. otro).
--       Drives template_checklists routing and assistant specialization.
alter table user_templates
  add column if not exists materia materia_legal;

-- 1.c · Subtype · finer classification within (materia × doc_type).
--       Examples: 'tutela_salud_eps', 'demanda_laboral_despido_injustificado',
--       'contestacion_excepcion_pago', 'contrato_arrendamiento_vivienda_urbana'
alter table user_templates
  add column if not exists subtype text;

-- 1.d · Applicable norms · array of normative references the template
--       relies on. The vigencia_checker can run against this on each
--       generation and warn / block if any cited norm has been derogated.
--       Example: ARRAY['Ley 1564/2012 art. 82', 'Const. art. 86']
alter table user_templates
  add column if not exists applicable_norms text[];

-- 1.e · Quality score · 0.00 to 1.00 · set by ingestion enrichment
--       (Sprint 2 batch pipeline) and updated by lawyer curator feedback.
alter table user_templates
  add column if not exists quality_score numeric(3,2)
    check (quality_score is null or (quality_score >= 0 and quality_score <= 1));

-- 1.f · Clauses · structured JSONB representation of the document's
--       sections (Hechos, Pretensiones, Fundamentos, etc.) so the
--       multi-agent generator can plan section-by-section.
--       Example: {"sections": [{"id": "hechos", "title": "Hechos",
--                                "required": true, "min_items": 3}, ...]}
alter table user_templates
  add column if not exists clauses_jsonb jsonb;

-- 1.g · Backlink to the RAG corpus · when a template is also indexed as
--       a `documents` row (so chunks are searchable in semantic retrieval),
--       this points at it for cross-reference. Optional · null = not indexed.
alter table user_templates
  add column if not exists ingest_doc_id uuid references documents(id) on delete set null;

-- 1.h · Usage analytics · cheap counters maintained by the generator on
--       each successful run · powers the "most used" sort in the picker.
alter table user_templates
  add column if not exists usage_count integer default 0 not null;
alter table user_templates
  add column if not exists last_used_at timestamptz;

-- Helpful indexes for the search picker + specialization query patterns.
create index if not exists user_templates_materia_kind_idx
  on user_templates (materia, doc_type)
  where firm_id is null;  -- system catalog hot path
create index if not exists user_templates_quality_idx
  on user_templates (quality_score desc nulls last)
  where firm_id is null and quality_score is not null;

-- ──────────────────────────────────────────────────────────────
-- 2. user_templates RLS · allow SYSTEM templates (firm_id IS NULL) to be
--    readable by every authenticated user. The existing select policy
--    (user_templates_select from sprint4 migration) only matches
--    firm_id = caller's firm · we ADD a second policy that grants read on
--    the system catalog. Both policies coexist (PostgreSQL OR-combines).
-- ──────────────────────────────────────────────────────────────
drop policy if exists user_templates_select_system on user_templates;
create policy user_templates_select_system on user_templates
  for select to authenticated
  using (firm_id is null);

-- Modification of system templates is RESTRICTED to service_role only.
-- We do NOT add a new ALL policy for `authenticated` on firm_id IS NULL ·
-- service_role bypasses RLS so the curator workflow (Sprint 4 admin UI)
-- writes via service-role-signed requests.

-- ──────────────────────────────────────────────────────────────
-- 3. template_checklists · YAML quality rules per (materia × kind)
-- ──────────────────────────────────────────────────────────────
-- Sprint 4 seeds 28 rows here (matrix of common materia × kind combos
-- for Colombia · laboral × demanda, civil × tutela, etc.).
-- The quality_validator loads the applicable checklist for each
-- generation and runs both programmatic checks and the LLM-as-judge
-- pass against it.
create table if not exists template_checklists (
  id              uuid primary key default gen_random_uuid(),
  materia         materia_legal not null,
  doc_type        text not null,
  jurisdiction    text not null default 'CO',
  -- YAML payload: list of rules · each rule has id, severity
  -- (blocking|warning|info), description, optional pattern/selector, and
  -- optional runner (e.g. 'vigencia_checker'). See docs.
  checklist_yaml  text not null,
  version         integer not null default 1,
  is_active       boolean not null default true,
  created_by      uuid references users(id) on delete set null,
  notes_md        text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  -- One active version per (materia, doc_type, jurisdiction, version).
  unique (materia, doc_type, jurisdiction, version)
);

create index if not exists template_checklists_lookup_idx
  on template_checklists (materia, doc_type, jurisdiction)
  where is_active = true;

alter table template_checklists enable row level security;

-- Readable by every authenticated user · curation through service_role.
drop policy if exists template_checklists_read on template_checklists;
create policy template_checklists_read on template_checklists
  for select to authenticated
  using (true);

-- ──────────────────────────────────────────────────────────────
-- 4. template_candidates · staging for scraped/uploaded templates
-- ──────────────────────────────────────────────────────────────
-- Populated by Sprint 2 ingestion pipeline (SECOP, Rama Judicial,
-- MinTrabajo scrapers). The curator (lawyer) reviews each row via
-- the Sprint 4 admin UI and decides approve / edit / reject / merge.
-- On approve, a row is inserted into user_templates with firm_id=NULL
-- (system catalog) and this candidate is marked status='approved'.
create table if not exists template_candidates (
  id                       uuid primary key default gen_random_uuid(),
  source                   text not null,           -- 'secop' | 'rama_judicial' | ...
  source_url               text,
  source_ref               text,                    -- original id / hash from source
  raw_storage_path         text,                    -- bucket: raw-templates
  normalized_storage_path  text,                    -- bucket: staged-templates
  -- Output of the gpt-4o-mini batch enrichment (Sprint 2)
  suggested_materia        materia_legal,
  suggested_doc_type       text,
  suggested_subtype        text,
  suggested_norms          text[],
  llm_report_jsonb         jsonb,                   -- {good[], risky[], missing[], summary}
  quality_score            numeric(3,2),
  dedup_cluster_id         uuid,                    -- groups near-duplicates
  status                   text not null default 'pending'
    check (status in ('pending','approved','rejected','merged','superseded')),
  reviewed_by              uuid references users(id) on delete set null,
  reviewed_at              timestamptz,
  approved_template_id     uuid references user_templates(id) on delete set null,
  notes_md                 text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create index if not exists template_candidates_status_idx
  on template_candidates (status, created_at desc);
create index if not exists template_candidates_dedup_idx
  on template_candidates (dedup_cluster_id)
  where dedup_cluster_id is not null;

alter table template_candidates enable row level security;
-- Only service_role / admin role accesses staging · no policy for
-- authenticated. Admin UI runs server-side with service-role key.

-- ──────────────────────────────────────────────────────────────
-- 5. template_generations · audit of multi-agent generator runs
-- ──────────────────────────────────────────────────────────────
-- This is the multi-agent equivalent of skill_executions (which is
-- single-shot). Each row spans an entire PLAN→DRAFT→CRITIC→EDIT→VALIDATE
-- flow and references the skill_executions rows for each LLM call.
create table if not exists template_generations (
  id                       uuid primary key default gen_random_uuid(),
  firm_id                  uuid not null references firms(id) on delete cascade,
  user_id                  uuid references users(id) on delete set null,
  matter_id                uuid,                    -- soft FK · matters live in another schema/table
  matter_document_id       uuid,                    -- soft FK to matter_documents
  template_id              uuid references user_templates(id) on delete set null,
  materia_at_generation    materia_legal,
  -- channel: how the generation was triggered
  channel                  text check (channel in ('voice','chat','cmdk','api')),
  -- which agents ran (subset for partial runs)
  stages_completed         text[] not null default '{}',
  -- per-stage timings for performance debugging
  timings_ms_jsonb         jsonb,
  checklist_score          numeric(3,2),
  checklist_failures       jsonb,
  judge_score              numeric(3,2),
  vigencia_warnings        jsonb,                   -- norms derogated detected
  fallback_to_freeform     boolean default false,
  hitl_decision            text,
  hitl_decision_payload    jsonb,
  total_tokens_input       integer default 0,
  total_tokens_output      integer default 0,
  total_cost_cents         integer default 0,
  status                   text not null default 'running'
    check (status in ('running','succeeded','blocked','failed','cancelled')),
  error_message            text,
  started_at               timestamptz not null default now(),
  completed_at             timestamptz
);

create index if not exists template_generations_firm_matter_idx
  on template_generations (firm_id, matter_id, started_at desc);
create index if not exists template_generations_template_idx
  on template_generations (template_id, started_at desc)
  where template_id is not null;

alter table template_generations enable row level security;

drop policy if exists template_generations_read on template_generations;
create policy template_generations_read on template_generations
  for select to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()));

drop policy if exists template_generations_write on template_generations;
create policy template_generations_write on template_generations
  for all to authenticated
  using (firm_id = (select firm_id from users where id = auth.uid()))
  with check (firm_id = (select firm_id from users where id = auth.uid()));

-- ──────────────────────────────────────────────────────────────
-- 6. updated_at triggers · only attach if the helper exists.
--    (Same defensive pattern as the original sprint4 migration.)
-- ──────────────────────────────────────────────────────────────
do $$
begin
  if exists (select 1 from pg_proc where proname = 'tg_set_updated_at') then
    drop trigger if exists template_checklists_set_updated_at on template_checklists;
    create trigger template_checklists_set_updated_at
      before update on template_checklists
      for each row execute function tg_set_updated_at();

    drop trigger if exists template_candidates_set_updated_at on template_candidates;
    create trigger template_candidates_set_updated_at
      before update on template_candidates
      for each row execute function tg_set_updated_at();
  end if;
end $$;

commit;

-- ============================================================
-- Post-apply sanity checks (run manually after migration):
-- ============================================================
-- 1.  select count(*) from user_templates;           -- unchanged from before
-- 2.  select column_name, is_nullable, data_type
--       from information_schema.columns
--      where table_name = 'user_templates'
--        and column_name in ('firm_id','materia','subtype','quality_score',
--                            'clauses_jsonb','applicable_norms',
--                            'ingest_doc_id','usage_count','last_used_at');
-- 3.  select pol.polname, c.relname
--       from pg_policy pol join pg_class c on c.oid = pol.polrelid
--      where c.relname in ('user_templates','template_checklists',
--                          'template_candidates','template_generations');
-- 4.  Try a SELECT as an authenticated user against user_templates
--     where firm_id IS NULL · should return rows (was 0 before).
-- ============================================================
