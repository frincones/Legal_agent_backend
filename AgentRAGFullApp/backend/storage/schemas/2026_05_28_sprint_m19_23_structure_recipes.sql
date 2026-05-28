-- ============================================================
-- LexAI · Sprint M19.23.A.1 · Structure Recipes Cache
-- Migration date: 2026-05-28
-- Idempotent · additive · no breaking changes
-- ============================================================
--
-- Tabla de cache para `structure_discovery` stage (M19.23.B).
-- Cada vez que el agente descubre la estructura de un documento (plan
-- de secciones + norma procesal aplicable + juez competente) basado en
-- la combinación (doc_type, jurisdiccion, cuantia_rango, demandado_tipo),
-- se almacena aquí para reuso. Próximas generaciones con la misma key
-- evitan la llamada al LLM y van directo a cache.
--
-- Reemplaza progresivamente los templates Python hardcoded (lex/templates/defs/)
-- sin eliminarlos — quedan como fallback de seguridad.

create table if not exists structure_recipes (
  id               uuid primary key default gen_random_uuid(),

  -- Clave compuesta normalizada (ej: "demanda_divorcio:familia:mayor:persona_natural")
  structure_key    text not null unique,

  -- Dimensiones que definen la estructura
  doc_type         text not null,
  jurisdiccion     text,                            -- 'civil'|'laboral'|'familia'|'admin'|'penal'|'constitucional'
  cuantia_rango    text,                            -- 'minima'|'menor'|'mayor'|'sin_cuantia'
  demandado_tipo   text,                            -- 'persona_natural'|'persona_juridica'|'entidad_publica'
  procedimiento    text,                            -- 'verbal_sumario'|'verbal'|'ordinario'|'abreviado'|'especial'

  -- El plan descubierto (lista de secciones)
  sections_plan    jsonb not null,                  -- [{key, title, order, roman, expected_blocks}]

  -- Metadata legal autoritativa
  norma_procesal_ref       text,                    -- "Art. 388-389 CGP (Ley 1564/2012)"
  juramento_norma_ref      text,                    -- "Art. 206 CGP"
  juez_competente          text,                    -- "Juez de Familia del Circuito"
  cuerpos_normativos_minimos jsonb default '[]'::jsonb, -- ["CGP", "CC", "Ley 1098/2006"]

  -- Trazabilidad
  fuentes_consultadas jsonb default '[]'::jsonb,    -- URLs verificadas por VerificationAgent
  generated_by    text default 'gpt-4o',            -- 'gpt-4o' | 'gpt-4o-mini' | 'fallback_template'
  generation_reasoning text,                        -- explicación del LLM para el plan

  -- Métricas de uso (para invalidación inteligente futura)
  usage_count     int default 0,
  approval_rate   float,                            -- ratio aprobado/total (se actualiza con feedback)
  last_used_at    timestamptz,

  created_at      timestamptz default now(),
  updated_at      timestamptz default now()
);

create index if not exists idx_structure_recipes_key
  on structure_recipes (structure_key);

create index if not exists idx_structure_recipes_doctype
  on structure_recipes (doc_type);

create index if not exists idx_structure_recipes_last_used
  on structure_recipes (last_used_at desc);

-- Trigger updated_at
create or replace function update_structure_recipes_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_structure_recipes_updated_at on structure_recipes;
create trigger trg_structure_recipes_updated_at
  before update on structure_recipes
  for each row execute function update_structure_recipes_updated_at();

comment on table structure_recipes is
  'Sprint M19.23.A.1: cache de planes de estructura descubiertos por el
   structure_discovery stage. Evita re-llamar al LLM para combinaciones
   recurrentes (doc_type, jurisdiccion, cuantia, demandado_tipo).';

comment on column structure_recipes.structure_key is
  'Clave única normalizada: lowercase, separada por : sin espacios.
   Ejemplo: "demanda_divorcio:familia:mayor:persona_natural".';

comment on column structure_recipes.sections_plan is
  'Array JSON ordenado de secciones del documento. Schema:
   [{"key": "encabezado", "title": "ENCABEZADO", "order": 1, "roman": null,
     "expected_blocks": ["paragraph", "section_heading"]}, ...]';
