-- ============================================================
-- LexAI · Sprint M19.24.F.1 · Invalidar recipes stale
-- Migration date: 2026-06-01
-- Idempotente · cleanup-only · no breaking changes
-- ============================================================
--
-- Las entries de structure_recipes generadas por M19.23 (antes de M19.24.A)
-- no tienen los 10 campos universales rellenos. Al servirse desde cache,
-- el block_generator universal NO recibe document_family/cierre_tipo/
-- playbooks/etc. y degrada a comportamiento legacy.
--
-- Esta migración:
--   1. Borra entries donde document_family IS NULL (recipes pre-M19.24).
--      No borra los anchors recién insertados (e_recipes_anchor) porque
--      esos sí tienen document_family poblado.
--   2. Próxima request hace cache MISS → llama structure_discovery LLM →
--      el nuevo prompt agnóstico produce los 10 campos → se cachean.
--
-- Es seguro: solo invalida cache. No toca datos generados ni docs.

DELETE FROM structure_recipes
WHERE document_family IS NULL
  AND created_at < '2026-05-30'::timestamptz;

-- Reset usage_count para los anchors pre-cargados (no han sido usados aún)
UPDATE structure_recipes
SET usage_count = 0, last_used_at = NULL
WHERE generated_by = 'manual_seed_m19_24'
  AND usage_count = 0;
