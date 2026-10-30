-- A relevance verdict must point to a document owned by the same web source.
-- The original single-column FK proves only that the document exists; this
-- composite FK also prevents cross-source document/version binding.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_web_document_source_document
    ON web_document (source_id, document_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_policy_web_source_validated_source_document'
          AND conrelid = 'policy_web_source'::regclass
    ) THEN
        ALTER TABLE policy_web_source
            ADD CONSTRAINT fk_policy_web_source_validated_source_document
            FOREIGN KEY (source_id, validated_document_id)
            REFERENCES web_document (source_id, document_id)
            ON DELETE RESTRICT;
    END IF;
END
$$;

COMMENT ON CONSTRAINT fk_policy_web_source_validated_source_document
    ON policy_web_source IS
    'Ensures a validation verdict is bound to a document from the same source.';

COMMIT;
