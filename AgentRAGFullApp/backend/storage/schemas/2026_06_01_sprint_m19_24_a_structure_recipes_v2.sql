-- ============================================================
-- LexAI · Sprint M19.24.A.1 · Structure Recipes v2 (universal)
-- Migration date: 2026-06-01
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Extiende structure_recipes (M19.23.A.1) con 10 columnas nuevas para
-- soportar CUALQUIER tipo de documento legal colombiano (no solo demandas).
--
-- Las columnas nuevas describen:
--   - document_family: categoria gruesa (judicial_demanda, notarial_poder, etc)
--   - regimen_aplicable: procesal/sustantivo/notarial/admin
--   - naturaleza_acto: declarativo/disposicion/mandato/etc
--   - encabezado_tipo: variante de header del documento
--   - cierre_tipo: variante de firma (5 opciones)
--   - numeracion_estilo: romana/clausulas/articulado/etc
--   - requires_*: booleans de secciones opcionales
--   - playbooks: instrucciones por seccion (reemplaza notas_calidad)
--
-- Migración no-destructiva: todas las columnas son nullable o tienen
-- default backwards-compatible. Los recipes existentes siguen sirviendo
-- con valores NULL en los nuevos campos (block_generator usa fallback).

ALTER TABLE structure_recipes
  ADD COLUMN IF NOT EXISTS document_family TEXT,
  ADD COLUMN IF NOT EXISTS regimen_aplicable TEXT,
  ADD COLUMN IF NOT EXISTS naturaleza_acto TEXT,
  ADD COLUMN IF NOT EXISTS encabezado_tipo TEXT,
  ADD COLUMN IF NOT EXISTS cierre_tipo TEXT,
  ADD COLUMN IF NOT EXISTS numeracion_estilo TEXT,
  ADD COLUMN IF NOT EXISTS requires_pretensiones BOOLEAN,
  ADD COLUMN IF NOT EXISTS requires_hechos BOOLEAN,
  ADD COLUMN IF NOT EXISTS requires_juramento BOOLEAN,
  ADD COLUMN IF NOT EXISTS playbooks JSONB DEFAULT '{}'::jsonb;

-- Index para búsqueda por familia (útil para discovery dinámico)
CREATE INDEX IF NOT EXISTS idx_structure_recipes_family
  ON structure_recipes (document_family);

CREATE INDEX IF NOT EXISTS idx_structure_recipes_regimen
  ON structure_recipes (regimen_aplicable);

-- Comentarios para documentación viva
COMMENT ON COLUMN structure_recipes.document_family IS
  'M19.24.A: Familia gruesa del documento. Enum:
   judicial_demanda, judicial_recurso, judicial_memorial, judicial_solicitud,
   judicial_constitucional, criminal_denuncia,
   notarial_poder, notarial_escritura, notarial_extrajuicio, notarial_acta,
   contractual_civil, contractual_mercantil, contractual_laboral, contractual_corporate,
   corporate_estatutos, corporate_acta, corporate_policy,
   petitorio_admin, petitorio_pqrs, petitorio_extrajudicial,
   tributario_dian, tributario_municipal,
   conceptual, sucesional, internacional, registro_publico, otro.';

COMMENT ON COLUMN structure_recipes.regimen_aplicable IS
  'M19.24.A: Régimen jurídico aplicable. Enum:
   procesal_judicial (CGP/CPACA/CPP/CPTSS), sustantivo_civil (CC),
   sustantivo_mercantil (CCo), notarial_extrajudicial (Decreto 960/1970),
   administrativo_publico (Ley 1437), tributario_dian (Estatuto Tributario),
   penal_acusatorio (Ley 906), laboral_sustantivo (CST), constitucional (CN).';

COMMENT ON COLUMN structure_recipes.naturaleza_acto IS
  'M19.24.A: Naturaleza jurídica del acto. Enum:
   declarativo, de_disposicion, de_administracion, de_garantia, de_mandato,
   petitorio, informativo, constitutivo, de_compromiso, extintivo.';

COMMENT ON COLUMN structure_recipes.encabezado_tipo IS
  'M19.24.A: Tipo de encabezado. Enum:
   memorial_juzgado, memorial_notario, memorial_autoridad_admin,
   carta_petitoria, instrumento_publico, comparecencia_partes,
   concepto_consultor, estatutos_societarios, acta_corporativa,
   politica_corporativa.';

COMMENT ON COLUMN structure_recipes.cierre_tipo IS
  'M19.24.A: Tipo de cierre/firma. Enum:
   firma_apoderado_judicial (Atentamente + T.P. C.S.J.),
   firma_partes_notarial (EL PODERDANTE / EL APODERADO ACEPTO),
   firma_natural (solo nombre + CC),
   diligencia_notarial (espacio reservado Notaría),
   firma_consultor (Cordialmente + cargo),
   firma_representante_legal (Rep. Legal + NIT),
   firma_partes_contractuales (LAS PARTES + nombre cada una),
   firma_corporativa_organos (Presidente + Secretario asamblea).
   Puede combinarse con + (ej "firma_partes_notarial+diligencia_notarial").';

COMMENT ON COLUMN structure_recipes.numeracion_estilo IS
  'M19.24.A: Estilo de numeración del documento. Enum:
   romana_secciones (I, II, III) tipico demanda,
   clausulas_ordinales (PRIMERA, SEGUNDA) tipico contrato/poder,
   articulado (Art. 1, Art. 2) tipico estatutos,
   numerico_simple (1, 2, 3),
   cronologico (sin numeración, párrafos consecutivos),
   alfabetico (a., b., c.).';

COMMENT ON COLUMN structure_recipes.playbooks IS
  'M19.24.A: Instrucciones específicas por section_key para guiar el
   block_generator. Schema:
   {
     "encabezado": ["dirige al notario", "identifica al poderdante con CC", ...],
     "facultades": ["enumera con cláusulas PRIMERA, SEGUNDA", ...]
   }
   Cada array son 3-7 bullets concretos. Reemplaza el SYSTEM_PROMPT
   hardcoded del block_generator que asumía demanda.';

COMMENT ON COLUMN structure_recipes.requires_pretensiones IS
  'M19.24.A: TRUE solo para demandas. FALSE para poderes/contratos/etc.';

COMMENT ON COLUMN structure_recipes.requires_hechos IS
  'M19.24.A: TRUE para demandas, recursos, denuncias. FALSE para poderes/contratos/conceptos.';

COMMENT ON COLUMN structure_recipes.requires_juramento IS
  'M19.24.A: TRUE solo cuando la norma procesal lo exige (Art. 206 CGP, etc).
   FALSE para tutela (Art. 86 CN), poderes, contratos, conceptos.';
