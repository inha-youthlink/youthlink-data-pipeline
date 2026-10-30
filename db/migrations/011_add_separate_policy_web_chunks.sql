-- Keep crawled web content in an independent RAG fallback layer.
-- This table never replaces or updates API-owned policy / policy_chunk data.

BEGIN;

CREATE TABLE IF NOT EXISTS policy_web_chunk (
    web_chunk_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_no              VARCHAR(30) NOT NULL,
    document_id            UUID NOT NULL,
    chunk_index            INTEGER NOT NULL,
    chunk_type             VARCHAR(30) NOT NULL DEFAULT 'WEB_TEXT',
    heading                TEXT,
    content                TEXT NOT NULL,
    content_hash           VARCHAR(64) NOT NULL,
    chunker_version        VARCHAR(50) NOT NULL,
    embedding              VECTOR(1536),
    embedding_model        VARCHAR(100),
    token_count            INTEGER,
    embedded_at            TIMESTAMPTZ,
    embedding_error        TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_policy_web_chunk_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT fk_policy_web_chunk_document
        FOREIGN KEY (document_id)
        REFERENCES web_document (document_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_policy_web_chunk_order
        UNIQUE (policy_no, document_id, chunk_index),

    CONSTRAINT uq_policy_web_chunk_content
        UNIQUE (policy_no, document_id, content_hash),

    CONSTRAINT chk_policy_web_chunk_index
        CHECK (chunk_index >= 0),

    CONSTRAINT chk_policy_web_chunk_type
        CHECK (chunk_type = 'WEB_TEXT'),

    CONSTRAINT chk_policy_web_chunk_content_not_blank
        CHECK (BTRIM(content) <> ''),

    CONSTRAINT chk_policy_web_chunk_content_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_policy_web_chunk_chunker_version
        CHECK (BTRIM(chunker_version) <> ''),

    CONSTRAINT chk_policy_web_chunk_token_count
        CHECK (token_count IS NULL OR token_count >= 0),

    CONSTRAINT chk_policy_web_chunk_embedding_state
        CHECK (
            (
                embedding IS NULL
                AND embedding_model IS NULL
                AND embedded_at IS NULL
            )
            OR
            (
                embedding IS NOT NULL
                AND embedding_model IS NOT NULL
                AND BTRIM(embedding_model) <> ''
                AND embedded_at IS NOT NULL
                AND embedding_error IS NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_web_chunk_policy_document
    ON policy_web_chunk (policy_no, document_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_policy_web_chunk_embedding_hnsw
    ON policy_web_chunk
    USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE VIEW v_verified_policy_web_chunk AS
SELECT
    chunk.web_chunk_id,
    chunk.policy_no,
    chunk.document_id,
    document.source_id,
    'CRAWLED_WEB'::TEXT AS source_type,
    source.canonical_url,
    source.final_url,
    document.title AS document_title,
    document.fetched_at,
    chunk.chunk_index,
    chunk.chunk_type,
    chunk.heading,
    chunk.content,
    chunk.content_hash,
    chunk.chunker_version,
    chunk.embedding,
    chunk.embedding_model,
    chunk.token_count,
    chunk.embedded_at,
    chunk.created_at,
    chunk.updated_at
FROM policy_web_chunk AS chunk
JOIN policy AS policy_row
  ON policy_row.policy_no = chunk.policy_no
JOIN web_document AS document
  ON document.document_id = chunk.document_id
JOIN web_source AS source
  ON source.source_id = document.source_id
WHERE policy_row.is_active = TRUE
  AND document.is_current = TRUE
  AND EXISTS (
      SELECT 1
      FROM policy_web_source AS link
      WHERE link.policy_no = chunk.policy_no
        AND link.source_id = document.source_id
        AND link.is_active = TRUE
        AND link.validation_status = 'VERIFIED'
        AND link.validated_document_id = document.document_id
  );

COMMENT ON TABLE policy_web_chunk IS
    'Verified crawled-web RAG fallback chunks; never merged into API policy_chunk.';

COMMENT ON VIEW v_verified_policy_web_chunk IS
    'Current verified crawled-web chunks safe for policy-scoped fallback retrieval.';

COMMIT;
