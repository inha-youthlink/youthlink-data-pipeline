-- YouthLink Prototype Database Schema
-- PostgreSQL + pgvector
--
-- Embedding model:
--   text-embedding-3-small
--   dimension: 1536
--   distance: cosine
--
-- Run db/extensions.sql before this file.

BEGIN;

CREATE TABLE IF NOT EXISTS common_code (
    code            VARCHAR(30) PRIMARY KEY,
    code_group      VARCHAR(50) NOT NULL,
    code_name       VARCHAR(100) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_common_code_group
    ON common_code (code_group);

CREATE TABLE IF NOT EXISTS policy_sync_job (
    sync_job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source                 VARCHAR(50) NOT NULL DEFAULT 'YOUTH_API',
    status                 VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    page_size              INTEGER,
    api_total_count        INTEGER,
    processed_count        INTEGER NOT NULL DEFAULT 0,
    success_count          INTEGER NOT NULL DEFAULT 0,
    failed_count           INTEGER NOT NULL DEFAULT 0,
    deactivated_count      INTEGER NOT NULL DEFAULT 0,
    full_scan_completed    BOOLEAN NOT NULL DEFAULT FALSE,
    deactivation_applied   BOOLEAN NOT NULL DEFAULT FALSE,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at            TIMESTAMPTZ,
    error_message          TEXT,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT chk_policy_sync_job_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')),

    CONSTRAINT chk_policy_sync_job_page_size
        CHECK (page_size IS NULL OR page_size > 0),

    CONSTRAINT chk_policy_sync_job_counts
        CHECK (
            (api_total_count IS NULL OR api_total_count >= 0)
            AND processed_count >= 0
            AND success_count >= 0
            AND failed_count >= 0
            AND deactivated_count >= 0
            AND processed_count = success_count + failed_count
        ),

    CONSTRAINT chk_policy_sync_job_finished_state
        CHECK (
            (status = 'RUNNING' AND finished_at IS NULL)
            OR (status <> 'RUNNING' AND finished_at IS NOT NULL)
        ),

    CONSTRAINT chk_policy_sync_job_deactivation_guard
        CHECK (
            NOT deactivation_applied
            OR (
                status = 'SUCCEEDED'
                AND full_scan_completed
                AND failed_count = 0
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_sync_job_started_at
    ON policy_sync_job (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_sync_job_status
    ON policy_sync_job (status, started_at DESC);

CREATE TABLE IF NOT EXISTS policy (
    policy_no                       VARCHAR(30) PRIMARY KEY,
    policy_name                     VARCHAR(300) NOT NULL,
    description                     TEXT,
    support_content                 TEXT,

    keywords                        TEXT[] NOT NULL DEFAULT '{}',
    large_categories                TEXT[] NOT NULL DEFAULT '{}',
    middle_categories               TEXT[] NOT NULL DEFAULT '{}',

    provider_group_code             VARCHAR(30),
    provision_method_code           VARCHAR(30),
    approval_status_code            VARCHAR(30),

    supervising_org_code            VARCHAR(30),
    supervising_org_name            VARCHAR(200),
    supervising_manager_name        VARCHAR(100),

    operating_org_code              VARCHAR(30),
    operating_org_name              VARCHAR(200),
    operating_manager_name          VARCHAR(100),

    support_scale_limit             BOOLEAN,
    support_scale_count             INTEGER,
    first_come_first_served         BOOLEAN,

    min_age                         INTEGER,
    max_age                         INTEGER,
    age_limit_yn                    BOOLEAN,

    marriage_status_code            VARCHAR(30),

    income_condition_code           VARCHAR(30),
    min_income                      BIGINT,
    max_income                      BIGINT,
    income_etc                      TEXT,

    application_period_type_code    VARCHAR(30),
    application_start_date          DATE,
    application_end_date            DATE,
    application_period_raw          TEXT,
    application_period_parse_status VARCHAR(20) NOT NULL DEFAULT 'EMPTY',

    business_period_type_code       VARCHAR(30),
    business_start_date             DATE,
    business_end_date               DATE,
    business_period_etc             TEXT,

    application_method              TEXT,
    screening_method                TEXT,
    submission_documents            TEXT,
    additional_notes                TEXT,
    additional_qualification        TEXT,
    participation_exclusion         TEXT,

    application_url                 TEXT,
    reference_url_1                 TEXT,
    reference_url_2                 TEXT,

    registrar_org_code              VARCHAR(30),
    registrar_org_name              VARCHAR(200),

    source_created_at               TIMESTAMPTZ,
    source_updated_at               TIMESTAMPTZ,
    synced_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    is_active                       BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at                  TIMESTAMPTZ,
    last_seen_sync_job_id           UUID,

    raw_data                        JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT fk_policy_last_seen_sync_job
        FOREIGN KEY (last_seen_sync_job_id)
        REFERENCES policy_sync_job (sync_job_id)
        ON DELETE SET NULL,

    CONSTRAINT chk_policy_active_state
        CHECK (
            (is_active AND deactivated_at IS NULL)
            OR (NOT is_active AND deactivated_at IS NOT NULL)
        ),

    CONSTRAINT chk_policy_age_range
        CHECK (min_age IS NULL OR max_age IS NULL OR min_age <= max_age),

    CONSTRAINT chk_policy_income_range
        CHECK (min_income IS NULL OR max_income IS NULL OR min_income <= max_income),

    CONSTRAINT chk_policy_application_period
        CHECK (
            application_start_date IS NULL
            OR application_end_date IS NULL
            OR application_start_date <= application_end_date
        ),

    CONSTRAINT chk_policy_application_period_parse_status
        CHECK (
            application_period_parse_status IN (
                'EMPTY', 'SINGLE', 'MULTIPLE', 'RAW', 'INVALID'
            )
        ),

    CONSTRAINT chk_policy_business_period
        CHECK (
            business_start_date IS NULL
            OR business_end_date IS NULL
            OR business_start_date <= business_end_date
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_name
    ON policy (policy_name);

CREATE INDEX IF NOT EXISTS idx_policy_age
    ON policy (min_age, max_age);

CREATE INDEX IF NOT EXISTS idx_policy_marriage_status
    ON policy (marriage_status_code);

CREATE INDEX IF NOT EXISTS idx_policy_application_period
    ON policy (application_start_date, application_end_date);

CREATE INDEX IF NOT EXISTS idx_policy_source_updated_at
    ON policy (source_updated_at);

CREATE INDEX IF NOT EXISTS idx_policy_last_seen_sync_job
    ON policy (last_seen_sync_job_id);

CREATE INDEX IF NOT EXISTS idx_policy_active_synced_at
    ON policy (synced_at DESC)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_policy_keywords_gin
    ON policy USING GIN (keywords);

CREATE INDEX IF NOT EXISTS idx_policy_large_categories_gin
    ON policy USING GIN (large_categories);

CREATE INDEX IF NOT EXISTS idx_policy_middle_categories_gin
    ON policy USING GIN (middle_categories);

CREATE TABLE IF NOT EXISTS policy_application_period (
    policy_no       VARCHAR(30) NOT NULL,
    period_index    INTEGER NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,

    PRIMARY KEY (policy_no, period_index),

    CONSTRAINT fk_policy_application_period_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT chk_policy_application_period_index
        CHECK (period_index >= 0),

    CONSTRAINT chk_policy_application_period_valid_date
        CHECK (
            start_date >= DATE '1900-01-01'
            AND end_date >= DATE '1900-01-01'
            AND start_date <= end_date
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_application_period_dates
    ON policy_application_period (start_date, end_date);

CREATE TABLE IF NOT EXISTS policy_region (
    policy_no       VARCHAR(30) NOT NULL,
    region_code     VARCHAR(20) NOT NULL,

    PRIMARY KEY (policy_no, region_code),

    CONSTRAINT fk_policy_region_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_policy_region_region_code
    ON policy_region (region_code);

CREATE TABLE IF NOT EXISTS policy_eligibility_code (
    id                  BIGSERIAL PRIMARY KEY,
    policy_no           VARCHAR(30) NOT NULL,
    condition_type      VARCHAR(20) NOT NULL,
    condition_code      VARCHAR(30) NOT NULL,

    CONSTRAINT fk_policy_eligibility_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT chk_policy_eligibility_type
        CHECK (condition_type IN ('MAJOR', 'JOB', 'SCHOOL', 'SPECIAL')),

    CONSTRAINT uq_policy_eligibility
        UNIQUE (policy_no, condition_type, condition_code)
);

CREATE INDEX IF NOT EXISTS idx_policy_eligibility_lookup
    ON policy_eligibility_code (condition_type, condition_code);

CREATE INDEX IF NOT EXISTS idx_policy_eligibility_policy
    ON policy_eligibility_code (policy_no);

CREATE TABLE IF NOT EXISTS policy_chunk (
    chunk_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_no           VARCHAR(30) NOT NULL,
    chunk_index         INTEGER NOT NULL,
    chunk_type          VARCHAR(30) NOT NULL,
    content             TEXT NOT NULL,

    content_hash        VARCHAR(64) NOT NULL,
    chunker_version     VARCHAR(50) NOT NULL,
    token_count         INTEGER,

    embedding           VECTOR(1536),
    embedding_model     VARCHAR(100),
    embedded_at         TIMESTAMPTZ,
    embedding_error     TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_policy_chunk_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT chk_policy_chunk_index
        CHECK (chunk_index >= 0),

    CONSTRAINT chk_policy_chunk_type
        CHECK (chunk_type IN ('SUMMARY', 'ELIGIBILITY', 'SUPPORT', 'APPLICATION')),

    CONSTRAINT chk_policy_chunk_content_not_blank
        CHECK (BTRIM(content) <> ''),

    CONSTRAINT chk_policy_chunk_content_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_policy_chunk_token_count
        CHECK (token_count IS NULL OR token_count >= 0),

    CONSTRAINT chk_policy_chunk_embedding_state
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
        ),

    CONSTRAINT uq_policy_chunk_order
        UNIQUE (policy_no, chunk_index),

    CONSTRAINT uq_policy_chunk_type
        UNIQUE (policy_no, chunk_type)
);

CREATE INDEX IF NOT EXISTS idx_policy_chunk_policy_no
    ON policy_chunk (policy_no);

CREATE INDEX IF NOT EXISTS idx_policy_chunk_type
    ON policy_chunk (chunk_type);

CREATE INDEX IF NOT EXISTS idx_policy_chunk_embedding_hnsw
    ON policy_chunk
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

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

CREATE UNIQUE INDEX IF NOT EXISTS uq_web_document_source_document
    ON web_document (source_id, document_id);

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

-- Crawled web content remains a separate RAG fallback corpus. It shares only
-- policy_no for scoped lookup and never updates API-owned policy_chunk rows.
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

CREATE TABLE IF NOT EXISTS chat_room (
    room_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(100) NOT NULL,
    title           VARCHAR(200),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_room_user_updated
    ON chat_room (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_profile (
    room_id                 UUID PRIMARY KEY,

    age                     INTEGER,
    gender                  VARCHAR(20),
    region_code             VARCHAR(20),
    marriage_status_code    VARCHAR(30),
    annual_income           BIGINT,

    major_code              VARCHAR(30),
    job_code                VARCHAR(30),
    school_code             VARCHAR(30),
    special_target_code     VARCHAR(30),

    extra_profile           JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_chat_profile_room
        FOREIGN KEY (room_id)
        REFERENCES chat_room (room_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_chat_profile_age
        CHECK (age IS NULL OR age >= 0),

    CONSTRAINT chk_chat_profile_income
        CHECK (annual_income IS NULL OR annual_income >= 0)
);

CREATE INDEX IF NOT EXISTS idx_chat_profile_region_code
    ON chat_profile (region_code);

CREATE TABLE IF NOT EXISTS chat_message (
    message_id       BIGSERIAL PRIMARY KEY,
    room_id          UUID NOT NULL,
    role             VARCHAR(20) NOT NULL,
    content          TEXT NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    client_message_id    UUID,
    reply_to_message_id  BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_chat_message_room
        FOREIGN KEY (room_id)
        REFERENCES chat_room (room_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_chat_message_reply_to
        FOREIGN KEY (reply_to_message_id)
        REFERENCES chat_message (message_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_chat_message_role
        CHECK (role IN ('user', 'assistant', 'system', 'tool')),

    CONSTRAINT uq_chat_message_client_message_id
        UNIQUE (room_id, client_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_message_room_created
    ON chat_message (room_id, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_message_reply_to
    ON chat_message (reply_to_message_id)
    WHERE reply_to_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS message_policy_ref (
    message_id          BIGINT NOT NULL,
    policy_no           VARCHAR(30) NOT NULL,
    relevance_score     DOUBLE PRECISION,
    display_order       SMALLINT NOT NULL,

    PRIMARY KEY (message_id, policy_no),

    CONSTRAINT fk_message_policy_ref_message
        FOREIGN KEY (message_id)
        REFERENCES chat_message (message_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_message_policy_ref_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT chk_message_policy_ref_score
        CHECK (
            relevance_score IS NULL
            OR (relevance_score >= -1.0 AND relevance_score <= 1.0)
        ),

    CONSTRAINT uq_message_policy_ref_order
        UNIQUE (message_id, display_order),

    CONSTRAINT chk_message_policy_ref_order
        CHECK (display_order >= 1)
);

CREATE INDEX IF NOT EXISTS idx_message_policy_ref_policy
    ON message_policy_ref (policy_no);

CREATE TABLE IF NOT EXISTS chat_current_policy (
    room_id         UUID NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    display_order   SMALLINT NOT NULL,

    PRIMARY KEY (room_id, policy_no),

    CONSTRAINT fk_chat_current_policy_room
        FOREIGN KEY (room_id)
        REFERENCES chat_room (room_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_chat_current_policy_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT uq_chat_current_policy_order
        UNIQUE (room_id, display_order),

    CONSTRAINT chk_chat_current_policy_order
        CHECK (display_order >= 1)
);

CREATE TABLE IF NOT EXISTS policy_scrap (
    user_id         VARCHAR(100) NOT NULL,
    policy_no       VARCHAR(30) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, policy_no),

    CONSTRAINT fk_policy_scrap_policy
        FOREIGN KEY (policy_no)
        REFERENCES policy (policy_no)
        ON DELETE CASCADE
);

COMMIT;
