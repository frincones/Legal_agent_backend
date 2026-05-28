-- ============================================================
-- LexAI · Sprint M19.24.C.1 · Legal Classifications Cache
-- Migration date: 2026-06-01
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Cache del legal_classifier (M19.24.B). Cada vez que el agente clasifica
-- conceptualmente un caso (régimen + naturaleza + correcciones de premisas
-- + advertencias de riesgo), se guarda aquí indexado por hash del prompt
-- normalizado. Prompts repetidos no re-pagan LLM.
--
-- Esto reproduce el "Paso 3" de Claude (reclasificación conceptual) y
-- el "Paso 2" (web search de fuentes oficiales).

CREATE TABLE IF NOT EXISTS legal_classifications_cache (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prompt_hash     TEXT NOT NULL UNIQUE,             -- sha256 del intent normalizado (lowercase + sin acentos)
  intent_preview  TEXT,                              -- primeros 300 chars del intent (debug)
  doc_type_hint   TEXT,                              -- doc_type que envió el frontend (puede diferir del verdadero)

  -- Outputs del LLM classifier
  document_family       TEXT NOT NULL,
  regimen_aplicable     TEXT,
  naturaleza_acto       TEXT,
  fundamento_normativo  JSONB DEFAULT '[]'::jsonb,    -- ["Arts. 2142 ss. CC", "Decreto 960/1970"]
  premisas_corregidas   JSONB DEFAULT '[]'::jsonb,    -- [{usuario_dijo, correcto, razon, fuente}]
  advertencias_riesgo   JSONB DEFAULT '[]'::jsonb,    -- ["Sin tope de cuantía el poder es de alto riesgo"]
  citas_verificadas     JSONB DEFAULT '[]'::jsonb,    -- [{ref, exists, max_in_law, suggested_correction}]

  -- Trazabilidad
  generated_by    TEXT DEFAULT 'gpt-4o',
  duration_ms     INT,
  reasoning       TEXT,

  -- Métricas de uso
  usage_count     INT DEFAULT 0,
  last_used_at    TIMESTAMPTZ DEFAULT NOW(),

  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_legal_classif_hash
  ON legal_classifications_cache (prompt_hash);

CREATE INDEX IF NOT EXISTS idx_legal_classif_family
  ON legal_classifications_cache (document_family);

CREATE INDEX IF NOT EXISTS idx_legal_classif_last_used
  ON legal_classifications_cache (last_used_at DESC);

-- Trigger updated_at
CREATE OR REPLACE FUNCTION update_legal_classif_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_legal_classif_updated_at ON legal_classifications_cache;
CREATE TRIGGER trg_legal_classif_updated_at
  BEFORE UPDATE ON legal_classifications_cache
  FOR EACH ROW EXECUTE FUNCTION update_legal_classif_updated_at();

COMMENT ON TABLE legal_classifications_cache IS
  'M19.24.B: cache del legal_classifier stage. Indexa por hash del prompt
   normalizado. Reproduce el Paso 3 de Claude (reclasificación conceptual
   con corrección de premisas legales del usuario).';

COMMENT ON COLUMN legal_classifications_cache.premisas_corregidas IS
  'Array de correcciones legales detectadas. Schema:
   [{
     "usuario_dijo": "Art. 836 del Código General del Proceso",
     "correcto": "Arts. 2142 y ss. del Código Civil + Decreto 960/1970",
     "razon": "El CGP llega hasta el Art. 627. El régimen del Art. 74 CGP
              es para poderes judiciales, no extrajudiciales.",
     "fuente": "secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html"
   }, ...]';

COMMENT ON COLUMN legal_classifications_cache.advertencias_riesgo IS
  'Array de advertencias estilo Claude. Cada string es una advertencia
   accionable: "Sin tope de cuantía el poder es de alto riesgo legal".';
