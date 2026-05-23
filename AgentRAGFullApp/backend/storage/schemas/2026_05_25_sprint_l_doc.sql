-- Sprint L-DOC: Document Generation V2 + Pipeline de Ingesta
-- ============================================================
-- Aplicar sobre Supabase Postgres. Todas las migraciones son ADDITIVE
-- (no destructive) — se pueden aplicar sin downtime.
--
-- Tablas creadas:
--   ingest_queue                 cola de trabajos de ingesta
--   ingest_runs                  log de sesiones de ingest
--   template_sections_catalog    secciones reutilizables con embedding
--   generated_document_sections  estado streaming por seccion
--   document_section_revisions   historial edits usuario
--   document_quality_scores      scorecard por documento
--   template_usage_stats         telemetria global
--
-- Vistas:
--   ingest_dashboard             stats agregadas por fuente
--
-- RLS: tablas multi-tenant tienen policy por firm_id.
--      tablas globales (ingest_queue, template_usage_stats) usan
--      acceso solo via service_role.

BEGIN;

-- ─── INGEST QUEUE ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest_queue (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text NOT NULL,
    url             text NOT NULL,
    url_hash        text NOT NULL,
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'skipped')),
    priority        integer DEFAULT 5,
    retries         integer DEFAULT 0,
    max_retries     integer DEFAULT 3,
    document_id     uuid REFERENCES documents(id) ON DELETE SET NULL,
    error_message   text,
    error_class     text,
    processing_ms   integer,
    discovered_at   timestamptz DEFAULT now(),
    started_at      timestamptz,
    completed_at    timestamptz,
    CONSTRAINT ingest_queue_url_hash_unique UNIQUE (url_hash)
);

CREATE INDEX IF NOT EXISTS ingest_queue_status_priority_idx
    ON ingest_queue (status, priority, discovered_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ingest_queue_source_status_idx
    ON ingest_queue (source, status);

-- Acceso restringido: solo service_role escribe, admins leen via endpoint
ALTER TABLE ingest_queue ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY ingest_queue_service_role ON ingest_queue
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── INGEST RUNS ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text NOT NULL,
    started_at      timestamptz DEFAULT now(),
    completed_at    timestamptz,
    docs_processed  integer DEFAULT 0,
    docs_failed     integer DEFAULT 0,
    docs_skipped    integer DEFAULT 0,
    cost_usd        numeric(10, 4) DEFAULT 0,
    stats_jsonb     jsonb NOT NULL DEFAULT '{}'::jsonb,
    triggered_by    text DEFAULT 'manual'  -- 'manual' | 'cron' | 'admin_ui'
);

CREATE INDEX IF NOT EXISTS ingest_runs_source_started_idx
    ON ingest_runs (source, started_at DESC);

ALTER TABLE ingest_runs ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY ingest_runs_service_role ON ingest_runs
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── PIPELINE LOGS ───────────────────────────────────────────────────
-- Logs operacionales del pipeline en una tabla ring-buffer (auto-truncate
-- a 5000 entradas mas recientes via trigger para no llenar Postgres).
CREATE TABLE IF NOT EXISTS pipeline_logs (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    level           text NOT NULL CHECK (level IN ('info', 'warn', 'error')),
    source          text NOT NULL,  -- source_id o 'system'
    job_id          text,
    message         text NOT NULL,
    context         jsonb
);

CREATE INDEX IF NOT EXISTS pipeline_logs_ts_idx
    ON pipeline_logs (ts DESC);

CREATE INDEX IF NOT EXISTS pipeline_logs_source_idx
    ON pipeline_logs (source, ts DESC);

CREATE INDEX IF NOT EXISTS pipeline_logs_level_idx
    ON pipeline_logs (level, ts DESC);

-- Auto-truncate trigger: mantener solo ultimas 5000 entradas
CREATE OR REPLACE FUNCTION pipeline_logs_truncate()
RETURNS TRIGGER AS $$
DECLARE
    cutoff_id bigint;
BEGIN
    -- Solo verificar cada 100 inserts (modulo) para no impactar perf
    IF NEW.id % 100 = 0 THEN
        SELECT id INTO cutoff_id FROM pipeline_logs
            ORDER BY id DESC OFFSET 5000 LIMIT 1;
        IF cutoff_id IS NOT NULL THEN
            DELETE FROM pipeline_logs WHERE id < cutoff_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tg_pipeline_logs_truncate ON pipeline_logs;
CREATE TRIGGER tg_pipeline_logs_truncate
    AFTER INSERT ON pipeline_logs
    FOR EACH ROW
    EXECUTE FUNCTION pipeline_logs_truncate();

ALTER TABLE pipeline_logs ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY pipeline_logs_service_role ON pipeline_logs
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── INGEST DASHBOARD VIEW ───────────────────────────────────────────
CREATE OR REPLACE VIEW ingest_dashboard AS
WITH stats AS (
    SELECT
        source,
        count(*) AS total,
        count(*) FILTER (WHERE status = 'pending') AS pending,
        count(*) FILTER (WHERE status = 'processing') AS processing,
        count(*) FILTER (WHERE status = 'completed') AS completed,
        count(*) FILTER (WHERE status = 'failed') AS failed,
        count(*) FILTER (WHERE status = 'skipped') AS skipped,
        sum(processing_ms) FILTER (WHERE status = 'completed') AS total_ms,
        avg(processing_ms) FILTER (WHERE status = 'completed') AS avg_ms,
        max(completed_at) FILTER (WHERE status = 'completed') AS last_completed,
        max(CASE WHEN status = 'failed' THEN error_message END) AS last_error
    FROM ingest_queue
    GROUP BY source
)
SELECT
    source,
    total,
    pending,
    processing,
    completed,
    failed,
    skipped,
    ROUND(100.0 * (completed + skipped) / NULLIF(total, 0), 1) AS pct_done,
    ROUND(avg_ms / 1000.0, 2) AS avg_seconds_per_doc,
    ROUND(total_ms / 60000.0, 1) AS total_minutes_spent,
    last_completed,
    CASE
        WHEN completed = total THEN NULL
        WHEN avg_ms IS NULL THEN NULL
        ELSE (now() + ((total - completed) * avg_ms * interval '1 millisecond'))
    END AS eta,
    last_error
FROM stats
ORDER BY pct_done DESC NULLS LAST;

-- ─── TEMPLATE SECTIONS CATALOG ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS template_sections_catalog (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id     uuid REFERENCES user_templates(id) ON DELETE CASCADE,
    section_key     text NOT NULL,
    section_title   text NOT NULL,
    section_order   integer NOT NULL,
    is_required     boolean DEFAULT true,
    content_md      text,
    instructions_md text,
    min_items       integer,
    variables_json  jsonb DEFAULT '[]'::jsonb,
    embedding       vector(1536),
    created_at      timestamptz DEFAULT now(),
    CONSTRAINT template_sections_unique_per_template UNIQUE (template_id, section_key)
);

CREATE INDEX IF NOT EXISTS template_sections_template_order_idx
    ON template_sections_catalog (template_id, section_order);

-- HNSW index para busqueda semantica de secciones similares
-- (defensive: solo crear si pgvector tiene HNSW disponible)
DO $$ BEGIN
    CREATE INDEX template_sections_embedding_hnsw_idx
        ON template_sections_catalog
        USING hnsw (embedding vector_cosine_ops);
EXCEPTION WHEN OTHERS THEN
    -- pgvector viejo no soporta HNSW, fallback a IVFFlat
    BEGIN
        EXECUTE 'CREATE INDEX IF NOT EXISTS template_sections_embedding_ivfflat_idx
            ON template_sections_catalog
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'No se pudo crear indice vectorial para template_sections_catalog';
    END;
END $$;

ALTER TABLE template_sections_catalog ENABLE ROW LEVEL SECURITY;

-- Visibility via user_templates.firm_id (system templates con NULL son publicos)
DO $$ BEGIN
    CREATE POLICY template_sections_tenant_visibility ON template_sections_catalog
        FOR SELECT
        USING (
            EXISTS (
                SELECT 1 FROM user_templates t
                WHERE t.id = template_sections_catalog.template_id
                  AND (t.firm_id IS NULL OR
                       t.firm_id::text = current_setting('request.jwt.claim.firm_id', true))
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY template_sections_service_role ON template_sections_catalog
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── GENERATED DOCUMENT SECTIONS ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS generated_document_sections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    generation_id   uuid NOT NULL,
    firm_id         uuid REFERENCES firms(id) ON DELETE CASCADE,
    matter_document_id uuid REFERENCES matter_documents(id) ON DELETE CASCADE,
    section_key     text NOT NULL,
    section_title   text NOT NULL,
    section_order   integer NOT NULL,
    content_md      text,
    stage           text DEFAULT 'draft'
        CHECK (stage IN ('draft', 'critic', 'edit', 'final')),
    critic_score    numeric(3, 2),
    critic_findings jsonb,
    citation_refs   text[],
    citation_status jsonb,
    streaming_done  boolean DEFAULT false,
    locked_by       uuid,
    locked_at       timestamptz,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gen_sections_generation_idx
    ON generated_document_sections (generation_id, section_order);

CREATE INDEX IF NOT EXISTS gen_sections_matter_doc_idx
    ON generated_document_sections (matter_document_id);

ALTER TABLE generated_document_sections ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY gen_sections_tenant_isolation ON generated_document_sections
        FOR ALL
        USING (firm_id::text = current_setting('request.jwt.claim.firm_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY gen_sections_service_role ON generated_document_sections
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── DOCUMENT SECTION REVISIONS ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_section_revisions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id      uuid REFERENCES generated_document_sections(id) ON DELETE CASCADE,
    firm_id         uuid REFERENCES firms(id) ON DELETE CASCADE,
    user_id         uuid,
    content_md      text,
    revision_type   text CHECK (revision_type IN (
        'agent_draft', 'user_edit', 'agent_regenerate',
        'user_accept', 'user_revert'
    )),
    delta_chars     integer DEFAULT 0,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS section_revisions_section_ts_idx
    ON document_section_revisions (section_id, created_at DESC);

ALTER TABLE document_section_revisions ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY section_revisions_tenant_isolation ON document_section_revisions
        FOR ALL
        USING (firm_id::text = current_setting('request.jwt.claim.firm_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY section_revisions_service_role ON document_section_revisions
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── DOCUMENT QUALITY SCORES ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_quality_scores (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    matter_document_id  uuid REFERENCES matter_documents(id) ON DELETE CASCADE,
    firm_id             uuid REFERENCES firms(id) ON DELETE CASCADE,
    generation_id       uuid,
    judge_score         numeric(3, 2),
    dimension_scores    jsonb,
    citation_rate       numeric(3, 2),
    user_rating         integer CHECK (user_rating BETWEEN 1 AND 5),
    user_feedback_md    text,
    computed_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS doc_quality_matter_doc_idx
    ON document_quality_scores (matter_document_id, computed_at DESC);

ALTER TABLE document_quality_scores ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY doc_quality_tenant_isolation ON document_quality_scores
        FOR ALL
        USING (firm_id::text = current_setting('request.jwt.claim.firm_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY doc_quality_service_role ON document_quality_scores
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── TEMPLATE USAGE STATS (global) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS template_usage_stats (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id         uuid REFERENCES user_templates(id) ON DELETE CASCADE UNIQUE,
    generations_count   integer DEFAULT 0,
    user_ratings_sum    integer DEFAULT 0,
    user_ratings_count  integer DEFAULT 0,
    avg_judge_score     numeric(3, 2),
    avg_citation_rate   numeric(3, 2),
    last_used_at        timestamptz,
    updated_at          timestamptz DEFAULT now()
);

ALTER TABLE template_usage_stats ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    CREATE POLICY template_usage_stats_read ON template_usage_stats
        FOR SELECT TO authenticated USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE POLICY template_usage_stats_service_role ON template_usage_stats
        FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;

-- ─── VALIDACION POST-MIGRACION ───────────────────────────────────────
-- Ejecutar manualmente despues de COMMIT para verificar:
--
-- SELECT table_name FROM information_schema.tables
--     WHERE table_schema='public'
--       AND table_name IN ('ingest_queue', 'ingest_runs', 'pipeline_logs',
--                          'template_sections_catalog', 'generated_document_sections',
--                          'document_section_revisions', 'document_quality_scores',
--                          'template_usage_stats');
-- Esperado: 8 filas.
--
-- SELECT * FROM ingest_dashboard LIMIT 5;
-- Esperado: vista existe, sin filas (queue vacia).
