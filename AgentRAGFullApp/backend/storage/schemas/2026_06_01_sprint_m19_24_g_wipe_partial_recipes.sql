-- ============================================================
-- LexAI · Sprint M19.24.G.1 · Wipe recipes con secciones incompletas
-- Migration date: 2026-06-01
-- Idempotente · cleanup-only · no breaking changes
-- ============================================================
--
-- Después de M19.24.F (que purgó entries sin document_family), las
-- entries regeneradas por structure_discovery LLM tampoco tenían
-- las secciones obligatorias (firma, vigencia, aceptacion).
-- M19.24.G refuerza el prompt + post-procesador para garantizarlas.
--
-- Esta migration purga las entries que se generaron entre F y G y
-- les faltan secciones obligatorias, para que se regeneren con la
-- estructura completa.

-- Borrar recipes generados por LLM (no manual_seed) que omiten 'firma'
DELETE FROM structure_recipes
WHERE generated_by IN ('gpt-4o', 'gpt-4o-mini', 'cache')
  AND NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements(sections_plan) AS sec
    WHERE sec->>'key' = 'firma'
  );

-- Borrar recipes notarial_poder que omiten vigencia o aceptacion
DELETE FROM structure_recipes
WHERE document_family = 'notarial_poder'
  AND generated_by IN ('gpt-4o', 'gpt-4o-mini', 'cache')
  AND (
    NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(sections_plan) AS sec
      WHERE sec->>'key' IN ('vigencia', 'vigencia_y_revocabilidad', 'vigencia_revocabilidad')
    )
    OR NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(sections_plan) AS sec
      WHERE sec->>'key' IN ('aceptacion', 'aceptacion_apoderado', 'aceptacion_del_apoderado')
    )
  );
