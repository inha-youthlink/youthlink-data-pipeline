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

    raw_data                        JSONB NOT NULL DEFAULT '{}'::jsonb,

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
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_chat_message_room
        FOREIGN KEY (room_id)
        REFERENCES chat_room (room_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_chat_message_role
        CHECK (role IN ('user', 'assistant', 'system', 'tool'))
);

CREATE INDEX IF NOT EXISTS idx_chat_message_room_created
    ON chat_message (room_id, created_at);

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

COMMIT;
