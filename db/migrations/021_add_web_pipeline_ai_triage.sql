BEGIN;

SET LOCAL search_path = pg_catalog, public;

ALTER TABLE public.web_pipeline_job
    DROP CONSTRAINT IF EXISTS chk_web_pipeline_step;

ALTER TABLE public.web_pipeline_job
    ADD CONSTRAINT chk_web_pipeline_step
    CHECK (current_step IN (
        'SOURCE_SYNC', 'CRAWL', 'REVALIDATE', 'AI_SHADOW',
        'AI_APPLY', 'AI_TRIAGE', 'WEB_CHUNKS', 'WEB_EMBEDDINGS',
        'RAG_PUBLISH', 'VERIFY', 'COMPLETED'
    ));

ALTER TABLE public.web_pipeline_step
    DROP CONSTRAINT IF EXISTS chk_web_pipeline_step_name;

ALTER TABLE public.web_pipeline_step
    ADD CONSTRAINT chk_web_pipeline_step_name
    CHECK (step_name IN (
        'SOURCE_SYNC', 'CRAWL', 'REVALIDATE', 'AI_SHADOW',
        'AI_APPLY', 'AI_TRIAGE', 'WEB_CHUNKS', 'WEB_EMBEDDINGS',
        'RAG_PUBLISH', 'VERIFY'
    ));

ALTER TABLE public.web_pipeline_step
    DROP CONSTRAINT IF EXISTS chk_web_pipeline_step_order;

ALTER TABLE public.web_pipeline_step
    ADD CONSTRAINT chk_web_pipeline_step_order
    CHECK (step_order BETWEEN 1 AND 10);

COMMIT;
