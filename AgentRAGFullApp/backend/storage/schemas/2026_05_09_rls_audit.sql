-- ============================================================
-- TASK-S0-07 · RLS AUDIT MIGRATION
-- ============================================================
-- Date: 2026-05-09
-- Purpose: Close RLS coverage gaps identified during the Sprint 0
--          audit. The multi-tenant tables (firms, users, matters, …)
--          and the agent observability tables (agent_traces, …) and
--          the per-user calc tables already have RLS enabled with
--          firm-scoped policies (lexai_multi_tenant_migration.sql,
--          2026_05_03_*.sql).
--
-- Gaps closed by this migration:
--   1. case_states            — no RLS; vulnerable to anon reads
--   2. conversations          — no RLS; per-session memory
--   3. conversation_chunks    — no RLS; embeddings of conversations
--   4. documents              — no RLS; RAG ingestion source docs
--   5. chunks                 — no RLS; RAG embeddings
--   6. normas                 — public legal catalogue: explicit
--                               read-allow + write deny-anon
--   7. derogaciones           — same
--   8. jurisprudencia         — same
--
-- Policy strategy:
--   - Tables with private/session data (1–5):
--       deny-all to anon. The backend talks to Postgres with the
--       Supabase service_role JWT which **bypasses RLS by design**,
--       so denying anon does not break ingestion or chat. If a
--       direct user-context client is ever introduced, add a row-
--       level matching policy at that point.
--   - Public legal catalogue (6–8):
--       SELECT allowed to any authenticated user; INSERT/UPDATE/
--       DELETE denied at row level (managed only via service_role).
--
-- Idempotent: uses IF EXISTS / DROP POLICY IF EXISTS so it can be
-- re-applied safely.
-- ============================================================

begin;

-- ──────────────────────────────────────────────────────────────
-- 1. case_states
-- ──────────────────────────────────────────────────────────────
alter table if exists case_states enable row level security;

drop policy if exists case_states_deny_anon_select on case_states;
create policy case_states_deny_anon_select on case_states
    for select to authenticated
    using (false);

drop policy if exists case_states_deny_anon_modify on case_states;
create policy case_states_deny_anon_modify on case_states
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 2. conversations
-- ──────────────────────────────────────────────────────────────
alter table if exists conversations enable row level security;

drop policy if exists conversations_deny_anon_select on conversations;
create policy conversations_deny_anon_select on conversations
    for select to authenticated
    using (false);

drop policy if exists conversations_deny_anon_modify on conversations;
create policy conversations_deny_anon_modify on conversations
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 3. conversation_chunks
-- ──────────────────────────────────────────────────────────────
alter table if exists conversation_chunks enable row level security;

drop policy if exists conversation_chunks_deny_anon_select on conversation_chunks;
create policy conversation_chunks_deny_anon_select on conversation_chunks
    for select to authenticated
    using (false);

drop policy if exists conversation_chunks_deny_anon_modify on conversation_chunks;
create policy conversation_chunks_deny_anon_modify on conversation_chunks
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 4. documents (RAG ingestion source docs)
-- ──────────────────────────────────────────────────────────────
alter table if exists documents enable row level security;

drop policy if exists documents_deny_anon_select on documents;
create policy documents_deny_anon_select on documents
    for select to authenticated
    using (false);

drop policy if exists documents_deny_anon_modify on documents;
create policy documents_deny_anon_modify on documents
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 5. chunks (RAG embeddings)
-- ──────────────────────────────────────────────────────────────
alter table if exists chunks enable row level security;

drop policy if exists chunks_deny_anon_select on chunks;
create policy chunks_deny_anon_select on chunks
    for select to authenticated
    using (false);

drop policy if exists chunks_deny_anon_modify on chunks;
create policy chunks_deny_anon_modify on chunks
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 6. normas — PUBLIC legal catalogue
-- ──────────────────────────────────────────────────────────────
alter table if exists normas enable row level security;

drop policy if exists normas_select_authenticated on normas;
create policy normas_select_authenticated on normas
    for select to authenticated
    using (true);

drop policy if exists normas_deny_anon_modify on normas;
create policy normas_deny_anon_modify on normas
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 7. derogaciones — PUBLIC legal catalogue
-- ──────────────────────────────────────────────────────────────
alter table if exists derogaciones enable row level security;

drop policy if exists derogaciones_select_authenticated on derogaciones;
create policy derogaciones_select_authenticated on derogaciones
    for select to authenticated
    using (true);

drop policy if exists derogaciones_deny_anon_modify on derogaciones;
create policy derogaciones_deny_anon_modify on derogaciones
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- 8. jurisprudencia — PUBLIC legal catalogue
-- ──────────────────────────────────────────────────────────────
alter table if exists jurisprudencia enable row level security;

drop policy if exists jurisprudencia_select_authenticated on jurisprudencia;
create policy jurisprudencia_select_authenticated on jurisprudencia
    for select to authenticated
    using (true);

drop policy if exists jurisprudencia_deny_anon_modify on jurisprudencia;
create policy jurisprudencia_deny_anon_modify on jurisprudencia
    for all to authenticated
    using (false) with check (false);

-- ──────────────────────────────────────────────────────────────
-- Verification helper · run in staging after applying:
--
--   select tablename,
--          rowsecurity   as rls_on,
--          (select count(*) from pg_policies p
--             where p.tablename = t.tablename) as policy_count
--     from pg_tables t
--    where schemaname = 'public'
--    order by tablename;
--
-- Expected: every table with private data must show rls_on=true and
-- policy_count >= 1.
-- ──────────────────────────────────────────────────────────────

commit;
