-- ============================================================
-- LexAI · Sprint M19.24.B.1 · Article Index Colombiano
-- Migration date: 2026-06-01
-- Idempotente · additive · no breaking changes
-- ============================================================
--
-- Tabla de validación de existencia de artículos por código.
-- Permite al legal_classifier detectar citas inexistentes como
-- "Art. 836 CGP" (el CGP solo llega al 627).
--
-- Cuando el usuario en su prompt cita "Art. X de Ley Y", el classifier
-- consulta esta tabla y si articulo > max_articulo emite una premisa
-- corregida tipo Claude:
--   "Art. 836 CGP no existe (el CGP llega al 627), el correcto es..."

CREATE TABLE IF NOT EXISTS article_index (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ley_codigo      TEXT NOT NULL,                  -- "CGP", "CC", "CST", "CN"
  ley_nombre      TEXT NOT NULL,                  -- "Código General del Proceso"
  ley_numero      TEXT,                            -- "Ley 1564 de 2012"
  ley_alias       JSONB DEFAULT '[]'::jsonb,      -- ["CGP", "C.G.P.", "Código General del Proceso", "Ley 1564"]
  max_articulo    INT NOT NULL,                    -- 627 para CGP
  min_articulo    INT DEFAULT 1,                   -- por si la ley empieza > 1
  articulos_omitidos JSONB DEFAULT '[]'::jsonb,   -- ej: [156, 157] derogados que NO existen ahora
  vigencia_desde  DATE,
  vigencia_hasta  DATE,                            -- null si vigente
  fuente_url      TEXT,                            -- secretariasenado.gov.co/...
  notas           TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (ley_codigo)
);

CREATE INDEX IF NOT EXISTS idx_article_index_codigo ON article_index(ley_codigo);

-- ============================================================
-- SEED: 18 cuerpos normativos colombianos principales
-- ============================================================

INSERT INTO article_index (ley_codigo, ley_nombre, ley_numero, ley_alias, max_articulo, fuente_url) VALUES
  ('CN', 'Constitución Política de Colombia', 'CN 1991',
   '["CN","C.N.","Constitución","Constitución Política","Constitución Política de 1991","Carta Magna"]'::jsonb,
   380, 'https://www.constitucioncolombia.com'),

  ('CGP', 'Código General del Proceso', 'Ley 1564 de 2012',
   '["CGP","C.G.P.","Código General del Proceso","Ley 1564","Ley 1564/2012","Ley 1564 de 2012"]'::jsonb,
   627, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1564_2012.html'),

  ('CC', 'Código Civil', 'Ley 84 de 1873',
   '["CC","C.C.","Código Civil","Ley 84"]'::jsonb,
   2684, 'https://www.secretariasenado.gov.co/senado/basedoc/codigo_civil.html'),

  ('CST', 'Código Sustantivo del Trabajo', 'Decreto 2663 de 1950',
   '["CST","C.S.T.","Código Sustantivo del Trabajo","Decreto 2663","Decreto 2663 de 1950"]'::jsonb,
   491, 'https://www.secretariasenado.gov.co/senado/basedoc/codigo_sustantivo_trabajo.html'),

  ('CPTSS', 'Código Procesal del Trabajo y de la Seguridad Social', 'Decreto 2158 de 1948',
   '["CPTSS","C.P.T.S.S.","Código Procesal del Trabajo","Decreto 2158"]'::jsonb,
   145, 'https://www.secretariasenado.gov.co'),

  ('CPACA', 'Código de Procedimiento Administrativo y de lo Contencioso Administrativo', 'Ley 1437 de 2011',
   '["CPACA","C.P.A.C.A.","Ley 1437","Ley 1437/2011","Ley 1437 de 2011"]'::jsonb,
   309, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1437_2011.html'),

  ('CPP', 'Código de Procedimiento Penal', 'Ley 906 de 2004',
   '["CPP","C.P.P.","Código de Procedimiento Penal","Ley 906","Ley 906/2004"]'::jsonb,
   538, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_0906_2004.html'),

  ('CP', 'Código Penal', 'Ley 599 de 2000',
   '["CP","C.P.","Código Penal","Ley 599","Ley 599/2000"]'::jsonb,
   478, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_0599_2000.html'),

  ('CCo', 'Código de Comercio', 'Decreto 410 de 1971',
   '["CCo","C.Co.","Código de Comercio","Decreto 410","Decreto 410/1971"]'::jsonb,
   2037, 'https://www.secretariasenado.gov.co/senado/basedoc/codigo_comercio.html'),

  ('CIA', 'Código de Infancia y Adolescencia', 'Ley 1098 de 2006',
   '["CIA","C.I.A.","Código de Infancia y Adolescencia","Código de la Infancia","Ley 1098","Ley 1098/2006"]'::jsonb,
   217, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1098_2006.html'),

  ('EN', 'Estatuto del Notariado y del Registro', 'Decreto 960 de 1970',
   '["EN","Estatuto Notarial","Estatuto del Notariado","Decreto 960","Decreto 960/1970","Decreto 960 de 1970"]'::jsonb,
   187, 'https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Decretos/1058919'),

  ('ET', 'Estatuto Tributario', 'Decreto 624 de 1989',
   '["ET","E.T.","Estatuto Tributario","Decreto 624","Decreto 624/1989"]'::jsonb,
   869, 'https://estatuto.co'),

  ('LEC', 'Ley de Enjuiciamiento Civil (derogada por CGP)', 'Ley 1564/2012 derogó',
   '["LEC"]'::jsonb,
   0, NULL),

  ('LEY472', 'Ley de Acciones Populares y de Grupo', 'Ley 472 de 1998',
   '["Ley 472","Ley 472/1998","Acciones Populares"]'::jsonb,
   88, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_0472_1998.html'),

  ('LEY1257', 'Ley contra Violencia hacia la Mujer', 'Ley 1257 de 2008',
   '["Ley 1257","Ley 1257/2008"]'::jsonb,
   45, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1257_2008.html'),

  ('LEY1581', 'Ley Estatutaria de Protección de Datos Personales', 'Ley 1581 de 2012',
   '["Ley 1581","Ley 1581/2012","LEPDP","Habeas Data"]'::jsonb,
   30, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1581_2012.html'),

  ('DEC2591', 'Decreto Reglamentario de la Tutela', 'Decreto 2591 de 1991',
   '["Decreto 2591","Decreto 2591/1991","Reglamentación Tutela"]'::jsonb,
   58, 'https://www.suin-juriscol.gov.co'),

  ('CDU', 'Código Disciplinario Único', 'Ley 1952 de 2019',
   '["CDU","C.D.U.","Código Disciplinario Único","Ley 1952","Ley 1952/2019"]'::jsonb,
   267, 'https://www.secretariasenado.gov.co/senado/basedoc/ley_1952_2019.html')
ON CONFLICT (ley_codigo) DO UPDATE SET
  max_articulo = EXCLUDED.max_articulo,
  ley_alias    = EXCLUDED.ley_alias,
  fuente_url   = EXCLUDED.fuente_url;

COMMENT ON TABLE article_index IS
  'M19.24.B: índice de validación de artículos. Permite detectar citas
   inexistentes como "Art. 836 CGP" antes de redactar el documento.
   El legal_classifier consulta esta tabla durante el pre-research.';
