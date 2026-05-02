-- ============================================
-- ACTIVITY METADATA — per-message timeline persistence
-- Adds a JSONB column to conversations that stores the full sequence of
-- NDJSON events emitted during the turn (status, ingest, vigencia,
-- jurisprudencia, sources, sourcerefs, clarify, case_shift, compact)
-- plus the case_index/turn_in_case markers needed to group activity by
-- case across multi-case sessions.
--
-- Idempotent: safe to re-run. Existing rows default to '{}' so older
-- code paths that never read this column continue to work.
-- ============================================

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS activity_metadata JSONB DEFAULT '{}'::jsonb;

-- GIN index on jsonb_path_ops accelerates future queries that filter by
-- nested keys (e.g. find every conversation that emitted a case_shift).
-- Optional — omit if Supabase Free's storage budget is tight.
CREATE INDEX IF NOT EXISTS conversations_activity_metadata_gin
    ON conversations USING GIN (activity_metadata jsonb_path_ops);
