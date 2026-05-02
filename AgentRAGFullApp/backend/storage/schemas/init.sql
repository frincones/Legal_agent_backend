-- Agent RAG Template - Database Schema
-- Compatible with Supabase (PostgreSQL + pgVector)

-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- DOCUMENTS TABLE
-- ============================================
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(500) NOT NULL,
    source VARCHAR(1000) NOT NULL,
    content TEXT,
    doc_type VARCHAR(50) DEFAULT 'unknown',
    metadata JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'completed',  -- 'pending' | 'processing' | 'completed' | 'failed'
    ingestion_error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

-- ============================================
-- CHUNKS TABLE
-- ============================================
CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector(1536),
    chunk_index INT DEFAULT 0,
    token_count INT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================
-- CONVERSATIONS TABLE (conversational memory)
-- ============================================
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id VARCHAR(200) NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT NOT NULL,
    intent VARCHAR(50),
    sources_used JSONB DEFAULT '[]',
    relevance_score FLOAT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at);

-- ============================================
-- CONVERSATION CHUNKS TABLE (embedded conversations for RAG)
-- ============================================
CREATE TABLE IF NOT EXISTS conversation_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector(1536),
    session_id VARCHAR(200) NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_chunks_session ON conversation_chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_conv_chunks_embedding ON conversation_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================
-- VECTOR SEARCH FUNCTION (documents)
-- ============================================
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding vector(1536),
    match_count INT DEFAULT 10,
    similarity_threshold FLOAT DEFAULT 0.7
)
RETURNS TABLE (
    id UUID,
    content TEXT,
    chunk_index INT,
    metadata JSONB,
    document_id UUID,
    document_title VARCHAR,
    document_source VARCHAR,
    similarity FLOAT
) AS $$
SELECT
    c.id,
    c.content,
    c.chunk_index,
    c.metadata,
    c.document_id,
    d.title AS document_title,
    d.source AS document_source,
    1 - (c.embedding <=> query_embedding) AS similarity
FROM chunks c
JOIN documents d ON c.document_id = d.id
WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
ORDER BY c.embedding <=> query_embedding
LIMIT match_count;
$$ LANGUAGE SQL STABLE;

-- ============================================
-- HYBRID SEARCH FUNCTION (vector + BM25 text)
-- ============================================
CREATE OR REPLACE FUNCTION hybrid_search(
    query_embedding vector(1536),
    query_text VARCHAR,
    match_count INT DEFAULT 10,
    text_weight FLOAT DEFAULT 0.3,
    similarity_threshold FLOAT DEFAULT 0.5
)
RETURNS TABLE (
    chunk_id UUID,
    document_id UUID,
    content TEXT,
    combined_score FLOAT,
    vector_similarity FLOAT,
    text_similarity FLOAT,
    metadata JSONB,
    document_title VARCHAR,
    document_source VARCHAR
) AS $$
WITH vector_scores AS (
    SELECT
        c.id AS chunk_id,
        c.document_id,
        c.content,
        c.metadata,
        1 - (c.embedding <=> query_embedding) AS vector_score
    FROM chunks c
    WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
),
text_scores AS (
    SELECT
        chunk_id,
        document_id,
        content,
        metadata,
        vector_score,
        ts_rank_cd(
            to_tsvector('english', content),
            plainto_tsquery('english', query_text)
        ) AS text_score
    FROM vector_scores
)
SELECT
    ts.chunk_id,
    ts.document_id,
    ts.content,
    ((1.0 - text_weight) * ts.vector_score + text_weight * COALESCE(ts.text_score, 0)) AS combined_score,
    ts.vector_score AS vector_similarity,
    COALESCE(ts.text_score, 0) AS text_similarity,
    ts.metadata,
    d.title AS document_title,
    d.source AS document_source
FROM text_scores ts
JOIN documents d ON ts.document_id = d.id
ORDER BY combined_score DESC
LIMIT match_count;
$$ LANGUAGE SQL STABLE;

-- ============================================
-- CONVERSATION SEARCH FUNCTION
-- ============================================
CREATE OR REPLACE FUNCTION match_conversations(
    query_embedding vector(1536),
    match_count INT DEFAULT 5,
    similarity_threshold FLOAT DEFAULT 0.7,
    target_session_id VARCHAR DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    content TEXT,
    session_id VARCHAR,
    conversation_id UUID,
    metadata JSONB,
    similarity FLOAT
) AS $$
SELECT
    cc.id,
    cc.content,
    cc.session_id,
    cc.conversation_id,
    cc.metadata,
    1 - (cc.embedding <=> query_embedding) AS similarity
FROM conversation_chunks cc
WHERE 1 - (cc.embedding <=> query_embedding) > similarity_threshold
    AND (target_session_id IS NULL OR cc.session_id = target_session_id)
ORDER BY cc.embedding <=> query_embedding
LIMIT match_count;
$$ LANGUAGE SQL STABLE;

-- ============================================
-- COMBINED SEARCH FUNCTION (documents + conversations)
-- ============================================
CREATE OR REPLACE FUNCTION match_all(
    query_embedding vector(1536),
    doc_match_count INT DEFAULT 5,
    conv_match_count INT DEFAULT 3,
    similarity_threshold FLOAT DEFAULT 0.7
)
RETURNS TABLE (
    id UUID,
    content TEXT,
    source_type VARCHAR,
    source_name VARCHAR,
    similarity FLOAT,
    metadata JSONB
) AS $$
WITH doc_results AS (
    SELECT
        c.id,
        c.content,
        'document'::VARCHAR AS source_type,
        d.title::VARCHAR AS source_name,
        1 - (c.embedding <=> query_embedding) AS similarity,
        c.metadata
    FROM chunks c
    JOIN documents d ON c.document_id = d.id
    WHERE 1 - (c.embedding <=> query_embedding) > similarity_threshold
    ORDER BY c.embedding <=> query_embedding
    LIMIT doc_match_count
),
conv_results AS (
    SELECT
        cc.id,
        cc.content,
        'conversation'::VARCHAR AS source_type,
        ('Session: ' || cc.session_id)::VARCHAR AS source_name,
        1 - (cc.embedding <=> query_embedding) AS similarity,
        cc.metadata
    FROM conversation_chunks cc
    WHERE 1 - (cc.embedding <=> query_embedding) > similarity_threshold
    ORDER BY cc.embedding <=> query_embedding
    LIMIT conv_match_count
)
SELECT * FROM doc_results
UNION ALL
SELECT * FROM conv_results;
$$ LANGUAGE SQL STABLE;
