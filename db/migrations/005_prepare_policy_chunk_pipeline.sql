BEGIN;

ALTER TABLE public.policy_chunk
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS chunker_version VARCHAR(50),
    ADD COLUMN IF NOT EXISTS token_count INTEGER,
    ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS embedding_error TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

-- Existing prototype rows can be migrated without losing their content or
-- embeddings. pgcrypto is already required by db/extensions.sql.
UPDATE public.policy_chunk
SET content_hash = ENCODE(DIGEST(content, 'sha256'), 'hex')
WHERE content_hash IS NULL;

UPDATE public.policy_chunk
SET chunker_version = 'legacy-v0'
WHERE chunker_version IS NULL;

UPDATE public.policy_chunk
SET updated_at = COALESCE(updated_at, created_at, NOW())
WHERE updated_at IS NULL;

UPDATE public.policy_chunk
SET
    embedding_model = NULL,
    embedded_at = NULL
WHERE embedding IS NULL;

UPDATE public.policy_chunk
SET
    embedding_model = COALESCE(embedding_model, 'text-embedding-3-small'),
    embedded_at = COALESCE(embedded_at, created_at, NOW()),
    embedding_error = NULL
WHERE embedding IS NOT NULL;

ALTER TABLE public.policy_chunk
    ALTER COLUMN content_hash SET NOT NULL,
    ALTER COLUMN chunker_version SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL,
    ALTER COLUMN embedding_model DROP DEFAULT,
    ALTER COLUMN embedding_model DROP NOT NULL;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'chk_policy_chunk_type'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT chk_policy_chunk_type
            CHECK (chunk_type IN ('SUMMARY', 'ELIGIBILITY', 'SUPPORT', 'APPLICATION'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'chk_policy_chunk_content_not_blank'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT chk_policy_chunk_content_not_blank
            CHECK (BTRIM(content) <> '');
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'chk_policy_chunk_content_hash'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT chk_policy_chunk_content_hash
            CHECK (content_hash ~ '^[0-9a-f]{64}$');
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'chk_policy_chunk_token_count'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT chk_policy_chunk_token_count
            CHECK (token_count IS NULL OR token_count >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'chk_policy_chunk_embedding_state'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT chk_policy_chunk_embedding_state
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
                    AND embedded_at IS NOT NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy_chunk'::regclass
          AND conname = 'uq_policy_chunk_type'
    ) THEN
        ALTER TABLE public.policy_chunk
            ADD CONSTRAINT uq_policy_chunk_type
            UNIQUE (policy_no, chunk_type);
    END IF;
END;
$migration$;

COMMIT;
