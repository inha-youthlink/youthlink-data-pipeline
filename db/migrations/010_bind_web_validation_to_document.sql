-- Bind every policy/source validation verdict to the exact document version
-- that was evaluated. A newer, unvalidated document must never inherit an
-- older VERIFIED relationship merely because it came from the same URL.

BEGIN;

ALTER TABLE policy_web_source
    ADD COLUMN IF NOT EXISTS validated_document_id UUID;

-- Backfill verdicts created by migration 009's first crawler implementation.
-- The source must match and only the current document can receive that verdict.
UPDATE policy_web_source AS link
SET validated_document_id = document.document_id
FROM web_document AS document
WHERE link.validation_status <> 'PENDING'
  AND link.validated_document_id IS NULL
  AND document.source_id = link.source_id
  AND document.is_current = TRUE;

-- If a legacy verdict has no document, it cannot remain trusted. Reset it to
-- PENDING instead of guessing which content was reviewed.
UPDATE policy_web_source
SET validation_status = 'PENDING',
    validation_score = NULL,
    matched_signals = '[]'::jsonb,
    conflicting_signals = '[]'::jsonb,
    validation_reason = NULL,
    validator_version = NULL,
    validated_at = NULL,
    validated_document_id = NULL,
    reviewed_by = NULL,
    reviewed_at = NULL
WHERE validation_status <> 'PENDING'
  AND validated_document_id IS NULL;

UPDATE policy_web_source
SET validated_document_id = NULL
WHERE validation_status = 'PENDING'
  AND validated_document_id IS NOT NULL;

ALTER TABLE policy_web_source
    DROP CONSTRAINT IF EXISTS chk_policy_web_source_validation_state;

ALTER TABLE policy_web_source
    ADD CONSTRAINT chk_policy_web_source_validation_state
    CHECK (
        (
            validation_status = 'PENDING'
            AND validated_at IS NULL
            AND validated_document_id IS NULL
        )
        OR (
            validation_status <> 'PENDING'
            AND validated_at IS NOT NULL
            AND validated_document_id IS NOT NULL
            AND validator_version IS NOT NULL
            AND BTRIM(validator_version) <> ''
        )
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_policy_web_source_validated_document'
          AND conrelid = 'policy_web_source'::regclass
    ) THEN
        ALTER TABLE policy_web_source
            ADD CONSTRAINT fk_policy_web_source_validated_document
            FOREIGN KEY (validated_document_id)
            REFERENCES web_document (document_id)
            ON DELETE RESTRICT;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_policy_web_source_validated_document
    ON policy_web_source (validated_document_id);

CREATE OR REPLACE VIEW v_verified_policy_web_document AS
SELECT DISTINCT
    link.policy_no,
    document.document_id,
    document.source_id,
    source.canonical_url,
    source.final_url,
    document.title,
    document.extracted_text,
    document.content_hash,
    document.extraction_method,
    document.extraction_version,
    document.language_code,
    document.published_at,
    document.fetched_at
FROM policy_web_source AS link
JOIN policy AS policy_row
  ON policy_row.policy_no = link.policy_no
JOIN web_source AS source
  ON source.source_id = link.source_id
JOIN web_document AS document
  ON document.document_id = link.validated_document_id
 AND document.source_id = source.source_id
WHERE policy_row.is_active = TRUE
  AND link.is_active = TRUE
  AND link.validation_status = 'VERIFIED'
  AND document.is_current = TRUE;

CREATE OR REPLACE VIEW v_verified_policy_enrichment AS
SELECT
    fact.fact_id,
    fact.policy_no,
    fact.document_id,
    document.source_id,
    source.canonical_url,
    document.title,
    fact.field_name,
    fact.value_json,
    fact.evidence_text,
    fact.evidence_section,
    fact.confidence,
    fact.extractor_version,
    fact.validated_at,
    fact.extracted_at
FROM policy_enrichment_fact AS fact
JOIN policy AS policy_row
  ON policy_row.policy_no = fact.policy_no
JOIN web_document AS document
  ON document.document_id = fact.document_id
JOIN web_source AS source
  ON source.source_id = document.source_id
WHERE policy_row.is_active = TRUE
  AND document.is_current = TRUE
  AND fact.validation_status = 'VERIFIED'
  AND EXISTS (
      SELECT 1
      FROM policy_web_source AS link
      WHERE link.policy_no = fact.policy_no
        AND link.source_id = document.source_id
        AND link.is_active = TRUE
        AND link.validation_status = 'VERIFIED'
        AND link.validated_document_id = document.document_id
  );

COMMIT;
