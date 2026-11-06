-- Store AI-assisted evidence observations for REVIEW_REQUIRED policy pages.
-- These rows are audit observations, not automatic authority: the canonical
-- policy_web_source decision is changed only by a separate guarded apply step.

BEGIN;

SET LOCAL search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS public.policy_web_ai_review (
    ai_review_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_no             VARCHAR(30) NOT NULL,
    source_id             UUID NOT NULL,
    document_id           UUID NOT NULL,
    content_hash          VARCHAR(64) NOT NULL,
    policy_evidence_hash  VARCHAR(64) NOT NULL,
    previous_status       VARCHAR(30) NOT NULL DEFAULT 'REVIEW_REQUIRED',

    model                 VARCHAR(100) NOT NULL,
    prompt_version        VARCHAR(100) NOT NULL,
    reviewer_version      VARCHAR(100) NOT NULL,
    execution_status      VARCHAR(30) NOT NULL,

    page_type             VARCHAR(30),
    identity_result       VARCHAR(30),
    raw_response          JSONB,
    identity_evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,
    supporting_evidence   JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicting_evidence  JSONB NOT NULL DEFAULT '[]'::jsonb,
    lifecycle_flags       JSONB NOT NULL DEFAULT '[]'::jsonb,
    rejected_evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,

    proposed_status       VARCHAR(30),
    proposed_score        NUMERIC(5, 4),
    verification_result   VARCHAR(30),
    failure_reason        TEXT NOT NULL,
    input_token_count     INTEGER,
    output_token_count    INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at            TIMESTAMPTZ,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT fk_policy_web_ai_review_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
        ON DELETE CASCADE,

    CONSTRAINT fk_policy_web_ai_review_source_document
        FOREIGN KEY (source_id, document_id)
        REFERENCES public.web_document (source_id, document_id)
        ON DELETE CASCADE,

    CONSTRAINT chk_policy_web_ai_review_content_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_policy_web_ai_review_policy_evidence_hash
        CHECK (policy_evidence_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT chk_policy_web_ai_review_previous_status
        CHECK (previous_status = 'REVIEW_REQUIRED'),

    CONSTRAINT chk_policy_web_ai_review_versions
        CHECK (
            BTRIM(model) <> ''
            AND BTRIM(prompt_version) <> ''
            AND BTRIM(reviewer_version) <> ''
        ),

    CONSTRAINT chk_policy_web_ai_review_execution_status
        CHECK (execution_status IN ('SUCCEEDED', 'FAILED', 'INVALID_OUTPUT')),

    CONSTRAINT chk_policy_web_ai_review_page_type
        CHECK (
            page_type IS NULL
            OR page_type IN (
                'DETAIL_SINGLE', 'LISTING_MULTI', 'PORTAL',
                'ERROR_OR_EMPTY', 'UNKNOWN'
            )
        ),

    CONSTRAINT chk_policy_web_ai_review_identity_result
        CHECK (
            identity_result IS NULL
            OR identity_result IN ('EXACT', 'CONFLICT', 'INSUFFICIENT')
        ),

    CONSTRAINT chk_policy_web_ai_review_json_arrays
        CHECK (
            jsonb_typeof(identity_evidence) = 'array'
            AND jsonb_typeof(supporting_evidence) = 'array'
            AND jsonb_typeof(conflicting_evidence) = 'array'
            AND jsonb_typeof(lifecycle_flags) = 'array'
            AND jsonb_typeof(rejected_evidence) = 'array'
        ),

    CONSTRAINT chk_policy_web_ai_review_raw_response
        CHECK (raw_response IS NULL OR jsonb_typeof(raw_response) = 'object'),

    CONSTRAINT chk_policy_web_ai_review_proposed_status
        CHECK (
            proposed_status IS NULL
            OR proposed_status IN ('VERIFIED', 'REVIEW_REQUIRED', 'REJECTED')
        ),

    CONSTRAINT chk_policy_web_ai_review_proposed_score
        CHECK (
            proposed_score IS NULL
            OR (proposed_score >= 0.0 AND proposed_score <= 1.0)
        ),

    CONSTRAINT chk_policy_web_ai_review_verification_result
        CHECK (
            verification_result IS NULL
            OR verification_result IN ('ACCEPTED', 'INSUFFICIENT')
        ),

    CONSTRAINT chk_policy_web_ai_review_execution_state
        CHECK (
            (
                execution_status = 'SUCCEEDED'
                AND page_type IS NOT NULL
                AND identity_result IS NOT NULL
                AND raw_response IS NOT NULL
                AND proposed_status IS NOT NULL
                AND proposed_score IS NOT NULL
                AND (
                    (
                        proposed_status IN ('VERIFIED', 'REJECTED')
                        AND verification_result = 'ACCEPTED'
                    )
                    OR (
                        proposed_status = 'REVIEW_REQUIRED'
                        AND verification_result = 'INSUFFICIENT'
                    )
                )
            )
            OR (
                execution_status IN ('FAILED', 'INVALID_OUTPUT')
                AND page_type IS NULL
                AND identity_result IS NULL
                AND proposed_status IS NULL
                AND proposed_score IS NULL
                AND verification_result IS NULL
                AND jsonb_array_length(identity_evidence) = 0
                AND jsonb_array_length(supporting_evidence) = 0
                AND jsonb_array_length(conflicting_evidence) = 0
                AND jsonb_array_length(lifecycle_flags) = 0
                AND jsonb_array_length(rejected_evidence) = 0
            )
        ),

    CONSTRAINT chk_policy_web_ai_review_failure_reason
        CHECK (BTRIM(failure_reason) <> ''),

    CONSTRAINT chk_policy_web_ai_review_token_counts
        CHECK (
            (input_token_count IS NULL OR input_token_count >= 0)
            AND (output_token_count IS NULL OR output_token_count >= 0)
        ),

    CONSTRAINT chk_policy_web_ai_review_metadata
        CHECK (jsonb_typeof(metadata) = 'object'),

    CONSTRAINT chk_policy_web_ai_review_applied_state
        CHECK (
            applied_at IS NULL
            OR (
                execution_status = 'SUCCEEDED'
                AND proposed_status IN ('VERIFIED', 'REJECTED')
                AND verification_result = 'ACCEPTED'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_web_ai_review_target
    ON public.policy_web_ai_review (policy_no, source_id, document_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_web_ai_review_success_cache
    ON public.policy_web_ai_review (
        policy_no,
        source_id,
        document_id,
        content_hash,
        policy_evidence_hash,
        model,
        prompt_version,
        reviewer_version
    )
    WHERE execution_status = 'SUCCEEDED';

CREATE INDEX IF NOT EXISTS idx_policy_web_ai_review_unapplied
    ON public.policy_web_ai_review (proposed_status, created_at)
    WHERE execution_status = 'SUCCEEDED'
      AND verification_result = 'ACCEPTED'
      AND applied_at IS NULL;

COMMENT ON TABLE public.policy_web_ai_review IS
    'Append-only AI evidence observations for exact REVIEW_REQUIRED document versions; canonical decisions require guarded application.';

COMMENT ON COLUMN public.policy_web_ai_review.raw_response IS
    'Parsed JSON returned by the model. It is untrusted until the code verifier accepts exact quotes against the stored document.';

COMMIT;
