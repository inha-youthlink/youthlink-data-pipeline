BEGIN;

SET LOCAL search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS public.schema_migration (
    migration_name VARCHAR(200) PRIMARY KEY,
    checksum_sha256 VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    execution_ms INTEGER,
    CONSTRAINT chk_schema_migration_checksum
        CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_schema_migration_execution
        CHECK (execution_ms IS NULL OR execution_ms >= 0)
);

CREATE TABLE IF NOT EXISTS public.policy_pipeline_job (
    pipeline_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    current_step VARCHAR(40) NOT NULL,
    resumed_from_job_id UUID,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    error_message TEXT,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_policy_pipeline_resume
        FOREIGN KEY (resumed_from_job_id)
        REFERENCES public.policy_pipeline_job (pipeline_job_id),
    CONSTRAINT chk_policy_pipeline_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT chk_policy_pipeline_step
        CHECK (current_step IN (
            'POLICY_SYNC', 'POLICY_CHUNKS', 'POLICY_EMBEDDINGS',
            'RAG_PUBLISH', 'VERIFY', 'COMPLETED'
        )),
    CONSTRAINT chk_policy_pipeline_finished
        CHECK (
            (status = 'RUNNING' AND finished_at IS NULL)
            OR (status IN ('SUCCEEDED', 'FAILED') AND finished_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_pipeline_job_started
    ON public.policy_pipeline_job (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_pipeline_job_status
    ON public.policy_pipeline_job (status, started_at DESC);

CREATE TABLE IF NOT EXISTS public.policy_pipeline_step (
    pipeline_step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_job_id UUID NOT NULL,
    step_name VARCHAR(40) NOT NULL,
    step_order SMALLINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    command_label TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    exit_code INTEGER,
    output_tail TEXT,
    CONSTRAINT fk_policy_pipeline_step_job
        FOREIGN KEY (pipeline_job_id)
        REFERENCES public.policy_pipeline_job (pipeline_job_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_policy_pipeline_step
        UNIQUE (pipeline_job_id, step_name),
    CONSTRAINT chk_policy_pipeline_step_name
        CHECK (step_name IN (
            'POLICY_SYNC', 'POLICY_CHUNKS', 'POLICY_EMBEDDINGS',
            'RAG_PUBLISH', 'VERIFY'
        )),
    CONSTRAINT chk_policy_pipeline_step_order
        CHECK (step_order BETWEEN 1 AND 5),
    CONSTRAINT chk_policy_pipeline_step_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT chk_policy_pipeline_step_finished
        CHECK (
            (status = 'RUNNING' AND finished_at IS NULL AND exit_code IS NULL)
            OR (
                status IN ('SUCCEEDED', 'FAILED')
                AND finished_at IS NOT NULL
                AND exit_code IS NOT NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_policy_pipeline_step_job
    ON public.policy_pipeline_step (pipeline_job_id, step_order);

COMMIT;
