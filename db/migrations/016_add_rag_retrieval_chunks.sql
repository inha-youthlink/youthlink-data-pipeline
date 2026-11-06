-- Publish API-owned and verified crawled-web chunks into one disposable
-- retrieval projection. Source-of-truth rows remain in policy_chunk and
-- policy_web_chunk; this table can always be rebuilt from those sources.

BEGIN;

SET LOCAL search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS public.rag_retrieval_chunk (
    retrieval_chunk_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_no             VARCHAR(30) NOT NULL,
    source_type           VARCHAR(30) NOT NULL,

    policy_chunk_id       UUID,
    web_chunk_id          UUID,
    document_id           UUID,
    source_id             UUID,
    canonical_url         TEXT,
    final_url             TEXT,
    source_fetched_at     TIMESTAMPTZ,

    chunk_index           INTEGER NOT NULL,
    chunk_type            VARCHAR(30) NOT NULL,
    heading               TEXT,
    content               TEXT NOT NULL,
    content_hash          VARCHAR(64) NOT NULL,
    chunker_version       VARCHAR(50) NOT NULL,

    embedding             VECTOR(1024) NOT NULL,
    embedding_model       VARCHAR(100) NOT NULL,
    token_count           INTEGER,
    embedded_at           TIMESTAMPTZ NOT NULL,

    source_updated_at     TIMESTAMPTZ NOT NULL,
    source_priority       SMALLINT NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_rag_retrieval_chunk_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT fk_rag_retrieval_chunk_policy_chunk
        FOREIGN KEY (policy_chunk_id)
        REFERENCES public.policy_chunk (chunk_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_rag_retrieval_chunk_web_chunk
        FOREIGN KEY (web_chunk_id)
        REFERENCES public.policy_web_chunk (web_chunk_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_rag_retrieval_chunk_document
        FOREIGN KEY (document_id)
        REFERENCES public.web_document (document_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_rag_retrieval_chunk_source
        FOREIGN KEY (source_id)
        REFERENCES public.web_source (source_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_rag_retrieval_chunk_policy_chunk
        UNIQUE (policy_chunk_id),

    CONSTRAINT uq_rag_retrieval_chunk_web_chunk
        UNIQUE (web_chunk_id),

    CONSTRAINT chk_rag_retrieval_chunk_origin
        CHECK (
            (
                source_type = 'API'
                AND policy_chunk_id IS NOT NULL
                AND web_chunk_id IS NULL
                AND document_id IS NULL
                AND source_id IS NULL
                AND canonical_url IS NULL
                AND final_url IS NULL
                AND source_fetched_at IS NULL
            )
            OR
            (
                source_type = 'CRAWLED_WEB'
                AND policy_chunk_id IS NULL
                AND web_chunk_id IS NOT NULL
                AND document_id IS NOT NULL
                AND source_id IS NOT NULL
                AND canonical_url IS NOT NULL
                AND BTRIM(canonical_url) <> ''
                AND source_fetched_at IS NOT NULL
                AND (final_url IS NULL OR BTRIM(final_url) <> '')
            )
        ),

    CONSTRAINT chk_rag_retrieval_chunk_type
        CHECK (
            (
                source_type = 'API'
                AND chunk_type IN (
                    'SUMMARY', 'ELIGIBILITY', 'SUPPORT', 'APPLICATION'
                )
            )
            OR
            (
                source_type = 'CRAWLED_WEB'
                AND chunk_type = 'WEB_TEXT'
            )
        ),

    CONSTRAINT chk_rag_retrieval_chunk_index
        CHECK (chunk_index >= 0),

    CONSTRAINT chk_rag_retrieval_chunk_content
        CHECK (BTRIM(content) <> ''),

    CONSTRAINT chk_rag_retrieval_chunk_content_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_rag_retrieval_chunk_chunker
        CHECK (BTRIM(chunker_version) <> ''),

    CONSTRAINT chk_rag_retrieval_chunk_model
        CHECK (BTRIM(embedding_model) <> ''),

    CONSTRAINT chk_rag_retrieval_chunk_token_count
        CHECK (token_count IS NULL OR token_count >= 0),

    CONSTRAINT chk_rag_retrieval_chunk_priority
        CHECK (source_priority BETWEEN 0 AND 1000)
);

CREATE INDEX IF NOT EXISTS idx_rag_retrieval_chunk_policy_source
    ON public.rag_retrieval_chunk (policy_no, source_type);

CREATE INDEX IF NOT EXISTS idx_rag_retrieval_chunk_embedding_hnsw
    ON public.rag_retrieval_chunk
    USING hnsw (embedding vector_cosine_ops);

COMMENT ON TABLE public.rag_retrieval_chunk IS
    'Disposable single-index RAG projection of active API chunks and current verified crawled-web chunks.';

COMMENT ON COLUMN public.rag_retrieval_chunk.source_type IS
    'Origin corpus: API or CRAWLED_WEB.';

COMMENT ON COLUMN public.rag_retrieval_chunk.source_priority IS
    'Source-aware reranking priority; similarity remains the primary retrieval signal.';

COMMIT;
