-- ============================================
-- LEGAL AGENT - Database Migration
-- Tablas para normas, derogaciones y jurisprudencia
-- Compatible con Supabase (PostgreSQL + pgVector)
-- ============================================

-- ============================================
-- NORMAS TABLE (Leyes, Decretos, Resoluciones)
-- ============================================
CREATE TABLE IF NOT EXISTS normas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo VARCHAR(50) NOT NULL,              -- LEY, DECRETO, RESOLUCION, CODIGO, CIRCULAR
    numero INTEGER,
    anio INTEGER,
    titulo TEXT,
    fecha_expedicion DATE,
    fecha_vigencia DATE,
    estado VARCHAR(20) DEFAULT 'VIGENTE',   -- VIGENTE, DEROGADA, MODIFICADA, SUSPENDIDA
    entidad_emisora VARCHAR(300),           -- Congreso, Ministerio del Trabajo, etc.
    sector VARCHAR(200),                    -- Laboral, Civil, Penal, Administrativo
    fuente VARCHAR(50),                     -- senado, funcion_publica, datos_gov, manual
    fuente_url TEXT,
    fuente_id VARCHAR(200),                 -- ID en la fuente (ej: i=18843 en Func.Publica)
    texto_completo TEXT,
    resumen TEXT,
    temas TEXT[] DEFAULT '{}',
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indices para normas
CREATE INDEX IF NOT EXISTS idx_normas_tipo_numero ON normas(tipo, numero, anio);
CREATE INDEX IF NOT EXISTS idx_normas_estado ON normas(estado);
CREATE INDEX IF NOT EXISTS idx_normas_sector ON normas(sector);
CREATE INDEX IF NOT EXISTS idx_normas_fecha ON normas(fecha_expedicion);
CREATE INDEX IF NOT EXISTS idx_normas_temas ON normas USING GIN(temas);
CREATE INDEX IF NOT EXISTS idx_normas_fuente ON normas(fuente);
CREATE INDEX IF NOT EXISTS idx_normas_embedding ON normas
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- Indice unico para evitar duplicados
CREATE UNIQUE INDEX IF NOT EXISTS idx_normas_unique
    ON normas(tipo, numero, anio) WHERE numero IS NOT NULL;

-- ============================================
-- DEROGACIONES TABLE (Grafo de derogaciones)
-- ============================================
CREATE TABLE IF NOT EXISTS derogaciones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    norma_origen_id UUID REFERENCES normas(id) ON DELETE CASCADE,   -- La que deroga/modifica
    norma_destino_id UUID REFERENCES normas(id) ON DELETE CASCADE,  -- La derogada/modificada
    tipo VARCHAR(30) NOT NULL,              -- DEROGA_TOTAL, DEROGA_PARCIAL, MODIFICA, ADICIONA, REGLAMENTA, SUSTITUYE
    articulos_afectados TEXT[] DEFAULT '{}', -- Articulos especificos afectados
    fecha_efecto DATE,
    fuente_texto TEXT,                      -- Texto del articulo que establece la derogacion
    detectado_por VARCHAR(20) DEFAULT 'manual', -- manual, auto_regex, auto_llm
    confianza FLOAT DEFAULT 1.0,            -- 0.0-1.0 confianza de la deteccion
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_derogaciones_origen ON derogaciones(norma_origen_id);
CREATE INDEX IF NOT EXISTS idx_derogaciones_destino ON derogaciones(norma_destino_id);
CREATE INDEX IF NOT EXISTS idx_derogaciones_tipo ON derogaciones(tipo);

-- Evitar duplicados de derogacion
CREATE UNIQUE INDEX IF NOT EXISTS idx_derogaciones_unique
    ON derogaciones(norma_origen_id, norma_destino_id, tipo);

-- ============================================
-- JURISPRUDENCIA TABLE (Sentencias)
-- ============================================
CREATE TABLE IF NOT EXISTS jurisprudencia (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    corte VARCHAR(50) NOT NULL,             -- CORTE_CONSTITUCIONAL, CORTE_SUPREMA, CONSEJO_ESTADO
    sala VARCHAR(100),                      -- Sala de Casacion Laboral, Sala Plena, etc.
    tipo_sentencia VARCHAR(10),             -- T (tutela), C (constitucionalidad), SU (unificacion), CASACION
    numero VARCHAR(100),                    -- T-067-25, C-200-19, etc.
    expediente VARCHAR(200),
    fecha DATE,
    magistrado TEXT,
    temas TEXT[] DEFAULT '{}',
    es_precedente BOOLEAN DEFAULT false,    -- true = vinculante, false = inter-partes
    normas_interpretadas TEXT[] DEFAULT '{}', -- Referencias a normas (ej: "LEY_1010_2006")
    ratio_decidendi TEXT,                   -- Regla juridica extraida
    obiter_dicta TEXT,                      -- Comentarios no vinculantes
    decision TEXT,                          -- Resumen de la decision
    fuente_url TEXT,
    fuente VARCHAR(50),                     -- datos_gov, relatoria_cc, corte_suprema
    texto_completo TEXT,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jurisprudencia_corte ON jurisprudencia(corte);
CREATE INDEX IF NOT EXISTS idx_jurisprudencia_tipo ON jurisprudencia(tipo_sentencia);
CREATE INDEX IF NOT EXISTS idx_jurisprudencia_fecha ON jurisprudencia(fecha);
CREATE INDEX IF NOT EXISTS idx_jurisprudencia_temas ON jurisprudencia USING GIN(temas);
CREATE INDEX IF NOT EXISTS idx_jurisprudencia_precedente ON jurisprudencia(es_precedente);
CREATE INDEX IF NOT EXISTS idx_jurisprudencia_embedding ON jurisprudencia
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

CREATE UNIQUE INDEX IF NOT EXISTS idx_jurisprudencia_unique
    ON jurisprudencia(corte, numero) WHERE numero IS NOT NULL;

-- ============================================
-- SEARCH FUNCTIONS
-- ============================================

-- Busqueda hibrida en normas (vector + texto)
CREATE OR REPLACE FUNCTION search_normas(
    query_embedding vector(1536),
    query_text VARCHAR DEFAULT '',
    filter_tipo VARCHAR DEFAULT NULL,
    filter_estado VARCHAR DEFAULT NULL,
    filter_sector VARCHAR DEFAULT NULL,
    match_limit INT DEFAULT 10,
    text_weight FLOAT DEFAULT 0.4,
    similarity_threshold FLOAT DEFAULT 0.1
)
RETURNS TABLE (
    norma_id UUID,
    tipo VARCHAR,
    numero INTEGER,
    anio INTEGER,
    titulo TEXT,
    estado VARCHAR,
    fecha_expedicion DATE,
    fuente_url TEXT,
    sector VARCHAR,
    resumen TEXT,
    content_preview TEXT,
    combined_score FLOAT,
    vector_similarity FLOAT,
    text_similarity FLOAT
) AS $$
WITH vector_scores AS (
    SELECT
        n.id AS norma_id,
        n.tipo,
        n.numero,
        n.anio,
        n.titulo,
        n.estado,
        n.fecha_expedicion,
        n.fuente_url,
        n.sector,
        n.resumen,
        LEFT(n.texto_completo, 500) AS content_preview,
        1 - (n.embedding <=> query_embedding) AS vector_score
    FROM normas n
    WHERE 1 - (n.embedding <=> query_embedding) > similarity_threshold
        AND (filter_tipo IS NULL OR n.tipo = filter_tipo)
        AND (filter_estado IS NULL OR n.estado = filter_estado)
        AND (filter_sector IS NULL OR n.sector = filter_sector)
),
text_scores AS (
    SELECT
        vs.*,
        ts_rank_cd(
            to_tsvector('spanish', COALESCE(vs.titulo, '') || ' ' || COALESCE(vs.content_preview, '')),
            plainto_tsquery('spanish', query_text)
        ) AS text_score
    FROM vector_scores vs
)
SELECT
    ts.norma_id,
    ts.tipo,
    ts.numero,
    ts.anio,
    ts.titulo,
    ts.estado,
    ts.fecha_expedicion,
    ts.fuente_url,
    ts.sector,
    ts.resumen,
    ts.content_preview,
    ((1.0 - text_weight) * ts.vector_score + text_weight * COALESCE(ts.text_score, 0)) AS combined_score,
    ts.vector_score AS vector_similarity,
    COALESCE(ts.text_score, 0) AS text_similarity
FROM text_scores ts
ORDER BY combined_score DESC
LIMIT match_limit;
$$ LANGUAGE SQL STABLE;

-- Busqueda hibrida en jurisprudencia
CREATE OR REPLACE FUNCTION search_jurisprudencia(
    query_embedding vector(1536),
    query_text VARCHAR DEFAULT '',
    filter_corte VARCHAR DEFAULT NULL,
    filter_tipo VARCHAR DEFAULT NULL,
    solo_precedentes BOOLEAN DEFAULT false,
    match_limit INT DEFAULT 10,
    text_weight FLOAT DEFAULT 0.4,
    similarity_threshold FLOAT DEFAULT 0.1
)
RETURNS TABLE (
    sentencia_id UUID,
    corte VARCHAR,
    tipo_sentencia VARCHAR,
    numero VARCHAR,
    fecha DATE,
    magistrado TEXT,
    es_precedente BOOLEAN,
    decision TEXT,
    ratio_decidendi TEXT,
    fuente_url TEXT,
    combined_score FLOAT,
    vector_similarity FLOAT,
    text_similarity FLOAT
) AS $$
WITH vector_scores AS (
    SELECT
        j.id AS sentencia_id,
        j.corte,
        j.tipo_sentencia,
        j.numero,
        j.fecha,
        j.magistrado,
        j.es_precedente,
        j.decision,
        j.ratio_decidendi,
        j.fuente_url,
        1 - (j.embedding <=> query_embedding) AS vector_score
    FROM jurisprudencia j
    WHERE 1 - (j.embedding <=> query_embedding) > similarity_threshold
        AND (filter_corte IS NULL OR j.corte = filter_corte)
        AND (filter_tipo IS NULL OR j.tipo_sentencia = filter_tipo)
        AND (solo_precedentes = false OR j.es_precedente = true)
),
text_scores AS (
    SELECT
        vs.*,
        ts_rank_cd(
            to_tsvector('spanish', COALESCE(vs.decision, '') || ' ' || COALESCE(vs.ratio_decidendi, '')),
            plainto_tsquery('spanish', query_text)
        ) AS text_score
    FROM vector_scores vs
)
SELECT
    ts.sentencia_id,
    ts.corte,
    ts.tipo_sentencia,
    ts.numero,
    ts.fecha,
    ts.magistrado,
    ts.es_precedente,
    ts.decision,
    ts.ratio_decidendi,
    ts.fuente_url,
    ((1.0 - text_weight) * ts.vector_score + text_weight * COALESCE(ts.text_score, 0)) AS combined_score,
    ts.vector_score AS vector_similarity,
    COALESCE(ts.text_score, 0) AS text_similarity
FROM text_scores ts
ORDER BY combined_score DESC
LIMIT match_limit;
$$ LANGUAGE SQL STABLE;

-- Verificar vigencia de una norma con cadena de derogaciones
CREATE OR REPLACE FUNCTION check_vigencia(
    check_tipo VARCHAR,
    check_numero INTEGER,
    check_anio INTEGER
)
RETURNS TABLE (
    norma_id UUID,
    tipo VARCHAR,
    numero INTEGER,
    anio INTEGER,
    titulo TEXT,
    estado VARCHAR,
    derogada_por_tipo VARCHAR,
    derogada_por_numero INTEGER,
    derogada_por_anio INTEGER,
    derogada_por_titulo TEXT,
    tipo_derogacion VARCHAR,
    fecha_efecto DATE,
    articulos_afectados TEXT[]
) AS $$
SELECT
    n.id AS norma_id,
    n.tipo,
    n.numero,
    n.anio,
    n.titulo,
    n.estado,
    n2.tipo AS derogada_por_tipo,
    n2.numero AS derogada_por_numero,
    n2.anio AS derogada_por_anio,
    n2.titulo AS derogada_por_titulo,
    d.tipo AS tipo_derogacion,
    d.fecha_efecto,
    d.articulos_afectados
FROM normas n
LEFT JOIN derogaciones d ON d.norma_destino_id = n.id
LEFT JOIN normas n2 ON n2.id = d.norma_origen_id
WHERE n.tipo = check_tipo
    AND n.numero = check_numero
    AND n.anio = check_anio
ORDER BY d.fecha_efecto DESC NULLS LAST;
$$ LANGUAGE SQL STABLE;

-- Obtener cadena completa de derogaciones (non-recursive for compatibility)
CREATE OR REPLACE FUNCTION get_derogation_chain(
    start_norma_id UUID,
    direction VARCHAR DEFAULT 'both'
)
RETURNS TABLE (
    norma_id UUID,
    tipo VARCHAR,
    numero INTEGER,
    anio INTEGER,
    titulo TEXT,
    estado VARCHAR,
    relacion VARCHAR,
    tipo_derogacion VARCHAR,
    depth INT
) AS $$
-- Origin
SELECT n.id, n.tipo, n.numero, n.anio, n.titulo, n.estado,
       'ORIGEN'::VARCHAR, NULL::VARCHAR, 0
FROM normas n WHERE n.id = start_norma_id

UNION ALL

-- Depth 1: who derogated me
SELECT n2.id, n2.tipo, n2.numero, n2.anio, n2.titulo, n2.estado,
       'DEROGADA_POR'::VARCHAR, d.tipo, 1
FROM derogaciones d
JOIN normas n2 ON n2.id = d.norma_origen_id
WHERE d.norma_destino_id = start_norma_id
    AND (direction = 'up' OR direction = 'both')

UNION ALL

-- Depth 1: who did I derogate
SELECT n2.id, n2.tipo, n2.numero, n2.anio, n2.titulo, n2.estado,
       'DEROGA_A'::VARCHAR, d.tipo, 1
FROM derogaciones d
JOIN normas n2 ON n2.id = d.norma_destino_id
WHERE d.norma_origen_id = start_norma_id
    AND (direction = 'down' OR direction = 'both')

UNION ALL

-- Depth 2: who derogated the ones that derogated me
SELECT n3.id, n3.tipo, n3.numero, n3.anio, n3.titulo, n3.estado,
       'DEROGADA_POR'::VARCHAR, d2.tipo, 2
FROM derogaciones d1
JOIN derogaciones d2 ON d2.norma_destino_id = d1.norma_origen_id
JOIN normas n3 ON n3.id = d2.norma_origen_id
WHERE d1.norma_destino_id = start_norma_id
    AND (direction = 'up' OR direction = 'both');
$$ LANGUAGE SQL STABLE;

-- Busqueda combinada: chunks RAG + normas + jurisprudencia
CREATE OR REPLACE FUNCTION search_legal_all(
    query_embedding vector(1536),
    query_text VARCHAR DEFAULT '',
    chunk_limit INT DEFAULT 10,
    norma_limit INT DEFAULT 5,
    juris_limit INT DEFAULT 5,
    similarity_threshold FLOAT DEFAULT 0.1
)
RETURNS TABLE (
    result_id UUID,
    content TEXT,
    source_type VARCHAR,
    source_name VARCHAR,
    similarity FLOAT,
    estado VARCHAR,
    metadata JSONB
) AS $$
-- Each UNION ALL branch must be parenthesized when it carries its own
-- ORDER BY / LIMIT; otherwise Postgres parses the ORDER BY as applying to
-- the whole UNION and rejects the LIMIT keyword at the next UNION.
(
    -- Chunks de documentos (RAG existente)
    SELECT
        c.id AS result_id,
        c.content,
        'document'::VARCHAR AS source_type,
        d.title::VARCHAR AS source_name,
        1 - (c.embedding <=> query_embedding) AS similarity,
        NULL::VARCHAR AS estado,
        c.metadata
    FROM chunks c
    JOIN documents d ON c.document_id = d.id
    WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
    ORDER BY c.embedding <=> query_embedding
    LIMIT chunk_limit
)

UNION ALL

(
    -- Normas legales
    SELECT
        n.id AS result_id,
        COALESCE(n.resumen, LEFT(n.texto_completo, 800)) AS content,
        'norma'::VARCHAR AS source_type,
        (n.tipo || ' ' || COALESCE(n.numero::TEXT, '') || ' de ' || COALESCE(n.anio::TEXT, ''))::VARCHAR AS source_name,
        1 - (n.embedding <=> query_embedding) AS similarity,
        n.estado,
        jsonb_build_object('fuente_url', n.fuente_url, 'sector', n.sector, 'temas', n.temas) AS metadata
    FROM normas n
    WHERE n.embedding IS NOT NULL
        AND 1 - (n.embedding <=> query_embedding) > similarity_threshold
    ORDER BY n.embedding <=> query_embedding
    LIMIT norma_limit
)

UNION ALL

(
    -- Jurisprudencia
    SELECT
        j.id AS result_id,
        COALESCE(j.ratio_decidendi, j.decision, LEFT(j.texto_completo, 800)) AS content,
        'jurisprudencia'::VARCHAR AS source_type,
        (j.corte || ' - ' || COALESCE(j.numero, ''))::VARCHAR AS source_name,
        1 - (j.embedding <=> query_embedding) AS similarity,
        CASE WHEN j.es_precedente THEN 'PRECEDENTE' ELSE 'INTER_PARTES' END AS estado,
        jsonb_build_object('fuente_url', j.fuente_url, 'magistrado', j.magistrado, 'temas', j.temas) AS metadata
    FROM jurisprudencia j
    WHERE j.embedding IS NOT NULL
        AND 1 - (j.embedding <=> query_embedding) > similarity_threshold
    ORDER BY j.embedding <=> query_embedding
    LIMIT juris_limit
);
$$ LANGUAGE SQL STABLE;
