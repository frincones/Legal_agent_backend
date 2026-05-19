-- Sprint P1 · skill_executions — columnas de trazabilidad de personalidad
-- ============================================================================
-- ADR-007 Fase 1 · TASK-F1-06 y TASK-F1-07
-- Solo ADD COLUMN IF NOT EXISTS. No toca nada más de skill_executions.
-- ============================================================================

alter table skill_executions
  add column if not exists personality_version_id uuid null;

alter table skill_executions
  add column if not exists personality_checksum text null;

comment on column skill_executions.personality_version_id is
  'Sprint P1 · FK a agent_personality_versions.id — qué versión de persona generó esta ejecución.';

comment on column skill_executions.personality_checksum is
  'Sprint P1 · SHA-256 del system prompt ensamblado. Permite correlacionar ejecuciones sin join.';
