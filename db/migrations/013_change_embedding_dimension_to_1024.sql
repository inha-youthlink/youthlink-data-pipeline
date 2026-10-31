-- Replace the existing 1536-dimensional embedding space with a new
-- 1024-dimensional model. Embeddings from different models or dimensions
-- are not comparable, so only embedding-derived state is discarded. Chunk
-- content, hashes, identifiers, and policy/document relationships remain.

BEGIN;

-- This view exposes policy_web_chunk.embedding and therefore must be removed
-- before PostgreSQL can change the column's vector typmod.
DROP VIEW IF EXISTS public.v_verified_policy_web_chunk;

DROP INDEX IF EXISTS public.idx_policy_chunk_embedding_hnsw;
DROP INDEX IF EXISTS public.idx_policy_web_chunk_embedding_hnsw;

-- Satisfy the embedding-state constraints before changing vector dimensions.
-- The type guard keeps this migration safe for a freshly-created 1024-dimension
-- schema and prevents an accidental rerun from erasing completed Titan vectors.
-- token_count is model/tokenizer-specific and is recalculated during the new
-- embedding run.
UPDATE public.policy_chunk
SET embedding = NULL,
    embedding_model = NULL,
    token_count = NULL,
    embedded_at = NULL,
    embedding_error = NULL
WHERE (
    SELECT format_type(attribute.atttypid, attribute.atttypmod)
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.policy_chunk'::regclass
      AND attribute.attname = 'embedding'
      AND NOT attribute.attisdropped
) = 'vector(1536)';

UPDATE public.policy_web_chunk
SET embedding = NULL,
    embedding_model = NULL,
    token_count = NULL,
    embedded_at = NULL,
    embedding_error = NULL
WHERE (
    SELECT format_type(attribute.atttypid, attribute.atttypmod)
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.policy_web_chunk'::regclass
      AND attribute.attname = 'embedding'
      AND NOT attribute.attisdropped
) = 'vector(1536)';

ALTER TABLE public.policy_chunk
    ALTER COLUMN embedding TYPE VECTOR(1024)
    USING CASE
        WHEN embedding IS NULL THEN NULL::VECTOR(1024)
        WHEN vector_dims(embedding) = 1024 THEN embedding::VECTOR(1024)
        ELSE NULL::VECTOR(1024)
    END;

ALTER TABLE public.policy_web_chunk
    ALTER COLUMN embedding TYPE VECTOR(1024)
    USING CASE
        WHEN embedding IS NULL THEN NULL::VECTOR(1024)
        WHEN vector_dims(embedding) = 1024 THEN embedding::VECTOR(1024)
        ELSE NULL::VECTOR(1024)
    END;

CREATE INDEX idx_policy_chunk_embedding_hnsw
    ON public.policy_chunk
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE INDEX idx_policy_web_chunk_embedding_hnsw
    ON public.policy_web_chunk
    USING hnsw (embedding vector_cosine_ops);

CREATE OR REPLACE VIEW public.v_verified_policy_web_chunk AS
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
FROM public.policy_web_chunk AS chunk
JOIN public.policy AS policy_row
  ON policy_row.policy_no = chunk.policy_no
JOIN public.web_document AS document
  ON document.document_id = chunk.document_id
JOIN public.web_source AS source
  ON source.source_id = document.source_id
WHERE policy_row.is_active = TRUE
  AND document.is_current = TRUE
  AND EXISTS (
      SELECT 1
      FROM public.policy_web_source AS link
      WHERE link.policy_no = chunk.policy_no
        AND link.source_id = document.source_id
        AND link.is_active = TRUE
        AND link.validation_status = 'VERIFIED'
        AND link.validated_document_id = document.document_id
  );

COMMENT ON VIEW public.v_verified_policy_web_chunk IS
    'Current verified crawled-web chunks safe for policy-scoped fallback retrieval.';

COMMIT;
