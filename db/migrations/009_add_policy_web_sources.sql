-- Store policy-linked web sources separately from the Youth API payload.
-- Fetched documents remain staging data until the policy/source relationship
-- and individual enrichment facts have been explicitly verified.

BEGIN;

CREATE TABLE IF NOT EXISTS web_source (
    source_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_url          TEXT NOT NULL UNIQUE,
    scheme                 VARCHAR(10) NOT NULL,
    host                   VARCHAR(253) NOT NULL,
    final_url              TEXT,
    fetch_status           VARCHAR(30) NOT NULL DEFAULT 'DISCOVERED',
    http_status            INTEGER,
    content_type           VARCHAR(255),
    etag                   TEXT,
    http_last_modified     TEXT,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_attempt_at        TIMESTAMPTZ,
    last_success_at        TIMESTAMPTZ,
    error_type             VARCHAR(100),
    error_message          TEXT,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT chk_web_source_url_not_blank
        CHECK (BTRIM(canonical_url) <> ''),

    CONSTRAINT chk_web_source_scheme
        CHECK (scheme IN ('http', 'https')),

    CONSTRAINT chk_web_source_host_not_blank
        CHECK (BTRIM(host) <> ''),

    CONSTRAINT chk_web_source_fetch_status
        CHECK (
            fetch_status IN (
                'DISCOVERED', 'FETCHING', 'FETCHED', 'NOT_MODIFIED',
                'UNSUPPORTED', 'BLOCKED', 'FAILED'
            )
        ),

    CONSTRAINT chk_web_source_http_status
        CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)
);

CREATE INDEX IF NOT EXISTS idx_web_source_host
    ON web_source (host);

CREATE INDEX IF NOT EXISTS idx_web_source_fetch_status
    ON web_source (fetch_status, last_attempt_at);

CREATE TABLE IF NOT EXISTS policy_web_source (
    policy_no              VARCHAR(30) NOT NULL,
    source_id              UUID NOT NULL,
    relation_type          VARCHAR(30) NOT NULL,
    original_url           TEXT NOT NULL,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at         TIMESTAMPTZ,
    validation_status      VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    validation_score       NUMERIC(5, 4),
    matched_signals        JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicting_signals    JSONB NOT NULL DEFAULT '[]'::jsonb,
    validation_reason      TEXT,
    validator_version      VARCHAR(100),
    validated_at           TIMESTAMPTZ,
    validated_document_id  UUID,
    reviewed_by            VARCHAR(100),
    reviewed_at            TIMESTAMPTZ,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (policy_no, source_id, relation_type),

    CONSTRAINT fk_policy_web_source_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT fk_policy_web_source_source
        FOREIGN KEY (source_id)
        REFERENCES web_source (source_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_policy_web_source_relation_type
        CHECK (
            relation_type IN (
                'APPLICATION', 'REFERENCE_1', 'REFERENCE_2'
            )
        ),

    CONSTRAINT chk_policy_web_source_original_url
        CHECK (BTRIM(original_url) <> ''),

    CONSTRAINT chk_policy_web_source_active_state
        CHECK (
            (is_active AND deactivated_at IS NULL)
            OR (NOT is_active AND deactivated_at IS NOT NULL)
        ),

    CONSTRAINT chk_policy_web_source_validation_status
        CHECK (
            validation_status IN (
                'PENDING', 'VERIFIED', 'REVIEW_REQUIRED',
                'REJECTED', 'LINK_ONLY'
            )
        ),

    CONSTRAINT chk_policy_web_source_validation_score
        CHECK (
            validation_score IS NULL
            OR (validation_score >= 0.0 AND validation_score <= 1.0)
        ),

    CONSTRAINT chk_policy_web_source_matched_signals
        CHECK (jsonb_typeof(matched_signals) = 'array'),

    CONSTRAINT chk_policy_web_source_conflicting_signals
        CHECK (jsonb_typeof(conflicting_signals) = 'array'),

    CONSTRAINT chk_policy_web_source_validation_state
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
        ),

    CONSTRAINT chk_policy_web_source_review_state
        CHECK (
            (reviewed_at IS NULL AND reviewed_by IS NULL)
            OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_web_source_source
    ON policy_web_source (source_id);

CREATE INDEX IF NOT EXISTS idx_policy_web_source_validation
    ON policy_web_source (validation_status, is_active, policy_no);

CREATE INDEX IF NOT EXISTS idx_policy_web_source_active_policy
    ON policy_web_source (policy_no, relation_type)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS web_document (
    document_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id              UUID NOT NULL,
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    title                  TEXT,
    extracted_text         TEXT NOT NULL,
    content_hash           VARCHAR(64) NOT NULL,
    extraction_method      VARCHAR(100) NOT NULL,
    extraction_version     VARCHAR(100) NOT NULL,
    language_code          VARCHAR(20),
    published_at           TIMESTAMPTZ,
    is_current             BOOLEAN NOT NULL DEFAULT TRUE,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_web_document_source
        FOREIGN KEY (source_id)
        REFERENCES web_source (source_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_web_document_content
        UNIQUE (source_id, content_hash),

    CONSTRAINT chk_web_document_text_not_blank
        CHECK (BTRIM(extracted_text) <> ''),

    CONSTRAINT chk_web_document_content_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_web_document_extraction_method
        CHECK (BTRIM(extraction_method) <> ''),

    CONSTRAINT chk_web_document_extraction_version
        CHECK (BTRIM(extraction_version) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_web_document_current_source
    ON web_document (source_id)
    WHERE is_current;

CREATE INDEX IF NOT EXISTS idx_web_document_fetched_at
    ON web_document (source_id, fetched_at DESC);

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

CREATE TABLE IF NOT EXISTS policy_enrichment_fact (
    fact_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_no              VARCHAR(30) NOT NULL,
    document_id            UUID NOT NULL,
    field_name             VARCHAR(100) NOT NULL,
    value_json             JSONB NOT NULL,
    evidence_text          TEXT NOT NULL,
    evidence_section       TEXT,
    fact_hash              VARCHAR(64) NOT NULL,
    confidence             NUMERIC(5, 4),
    validation_status      VARCHAR(30) NOT NULL DEFAULT 'EXTRACTED',
    extractor_version      VARCHAR(100) NOT NULL,
    validation_reason      TEXT,
    validated_at           TIMESTAMPTZ,
    reviewed_by            VARCHAR(100),
    reviewed_at            TIMESTAMPTZ,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    extracted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_policy_enrichment_fact_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT fk_policy_enrichment_fact_document
        FOREIGN KEY (document_id)
        REFERENCES web_document (document_id)
        ON DELETE CASCADE,

    CONSTRAINT uq_policy_enrichment_fact
        UNIQUE (policy_no, document_id, field_name, fact_hash),

    CONSTRAINT chk_policy_enrichment_fact_field
        CHECK (BTRIM(field_name) <> ''),

    CONSTRAINT chk_policy_enrichment_fact_evidence
        CHECK (BTRIM(evidence_text) <> ''),

    CONSTRAINT chk_policy_enrichment_fact_hash
        CHECK (fact_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_policy_enrichment_fact_confidence
        CHECK (
            confidence IS NULL
            OR (confidence >= 0.0 AND confidence <= 1.0)
        ),

    CONSTRAINT chk_policy_enrichment_fact_status
        CHECK (
            validation_status IN (
                'EXTRACTED', 'VERIFIED', 'REJECTED',
                'CONFLICT', 'NOT_APPLICABLE'
            )
        ),

    CONSTRAINT chk_policy_enrichment_fact_extractor
        CHECK (BTRIM(extractor_version) <> ''),

    CONSTRAINT chk_policy_enrichment_fact_validation_state
        CHECK (
            (
                validation_status = 'EXTRACTED'
                AND validated_at IS NULL
            )
            OR (
                validation_status <> 'EXTRACTED'
                AND validated_at IS NOT NULL
            )
        ),

    CONSTRAINT chk_policy_enrichment_fact_review_state
        CHECK (
            (reviewed_at IS NULL AND reviewed_by IS NULL)
            OR (reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_enrichment_fact_policy_field
    ON policy_enrichment_fact (policy_no, field_name);

CREATE INDEX IF NOT EXISTS idx_policy_enrichment_fact_validation
    ON policy_enrichment_fact (validation_status, policy_no);

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
