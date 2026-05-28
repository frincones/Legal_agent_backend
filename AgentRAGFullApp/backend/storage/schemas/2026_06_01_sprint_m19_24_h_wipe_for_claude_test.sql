-- ============================================================
-- LexAI · Sprint M19.24.H.1 · Wipe caches para test con Claude API
-- Migration date: 2026-06-01
-- Idempotente · cleanup-only · no breaking changes
-- ============================================================
--
-- Las entries de legal_classifications_cache y structure_recipes
-- generadas con OpenAI GPT-4o se sirven de cache aunque ahora el
-- routing apunte a Anthropic Claude. Para el A/B test necesitamos
-- que se regeneren con Claude.
--
-- Esta migration purga:
--   - legal_classifications_cache completo (es rápido regenerar, 3-8s)
--   - structure_recipes generados por gpt-4o (no manual_seed_m19_24)
--
-- Es seguro: solo cache. No toca documentos generados.

DELETE FROM legal_classifications_cache;

DELETE FROM structure_recipes
WHERE generated_by IN ('gpt-4o', 'gpt-4o-mini')
  AND created_at >= '2026-05-28'::date;
