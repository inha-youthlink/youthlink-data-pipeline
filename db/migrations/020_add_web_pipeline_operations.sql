BEGIN;

SET LOCAL search_path = pg_catalog, public;

CREATE TABLE IF NOT EXISTS public.web_pipeline_job (
    web_pipeline_job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    current_step VARCHAR(40) NOT NULL,
    resumed_from_job_id UUID,
    crawl_limit INTEGER NOT NULL,
    per_host_limit INTEGER NOT NULL,
    ai_review_limit INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    error_message TEXT,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT fk_web_pipeline_resume
        FOREIGN KEY (resumed_from_job_id)
        REFERENCES public.web_pipeline_job (web_pipeline_job_id),
    CONSTRAINT chk_web_pipeline_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT chk_web_pipeline_step
        CHECK (current_step IN (
            'SOURCE_SYNC', 'CRAWL', 'REVALIDATE', 'AI_SHADOW',
            'AI_APPLY', 'WEB_CHUNKS', 'WEB_EMBEDDINGS',
            'RAG_PUBLISH', 'VERIFY', 'COMPLETED'
        )),
    CONSTRAINT chk_web_pipeline_limits
        CHECK (
            crawl_limit > 0
            AND per_host_limit > 0
            AND ai_review_limit > 0
        ),
    CONSTRAINT chk_web_pipeline_finished
        CHECK (
            (status = 'RUNNING' AND finished_at IS NULL)
            OR (status IN ('SUCCEEDED', 'FAILED') AND finished_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_web_pipeline_job_started
    ON public.web_pipeline_job (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_web_pipeline_job_status
    ON public.web_pipeline_job (status, started_at DESC);

CREATE TABLE IF NOT EXISTS public.web_pipeline_step (
    web_pipeline_step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    web_pipeline_job_id UUID NOT NULL,
    step_name VARCHAR(40) NOT NULL,
    step_order SMALLINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
    command_label TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    exit_code INTEGER,
    output_tail TEXT,
    CONSTRAINT fk_web_pipeline_step_job
        FOREIGN KEY (web_pipeline_job_id)
        REFERENCES public.web_pipeline_job (web_pipeline_job_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_web_pipeline_step
        UNIQUE (web_pipeline_job_id, step_name),
    CONSTRAINT chk_web_pipeline_step_name
        CHECK (step_name IN (
            'SOURCE_SYNC', 'CRAWL', 'REVALIDATE', 'AI_SHADOW',
            'AI_APPLY', 'WEB_CHUNKS', 'WEB_EMBEDDINGS',
            'RAG_PUBLISH', 'VERIFY'
        )),
    CONSTRAINT chk_web_pipeline_step_order
        CHECK (step_order BETWEEN 1 AND 9),
    CONSTRAINT chk_web_pipeline_step_status
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT chk_web_pipeline_step_finished
        CHECK (
            (status = 'RUNNING' AND finished_at IS NULL AND exit_code IS NULL)
            OR (
                status IN ('SUCCEEDED', 'FAILED')
                AND finished_at IS NOT NULL
                AND exit_code IS NOT NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_web_pipeline_step_job
    ON public.web_pipeline_step (web_pipeline_job_id, step_order);

COMMIT;
