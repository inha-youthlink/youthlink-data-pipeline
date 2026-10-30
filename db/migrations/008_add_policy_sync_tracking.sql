-- Track complete Youth API sync jobs and preserve policies that disappear from
-- the source as inactive rows. Existing policies remain active until a later
-- complete, failure-free API sync explicitly marks unseen policies inactive.

BEGIN;

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

ALTER TABLE policy
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_seen_sync_job_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy'::regclass
          AND conname = 'fk_policy_last_seen_sync_job'
    ) THEN
        ALTER TABLE policy
            ADD CONSTRAINT fk_policy_last_seen_sync_job
            FOREIGN KEY (last_seen_sync_job_id)
            REFERENCES policy_sync_job (sync_job_id)
            ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy'::regclass
          AND conname = 'chk_policy_active_state'
    ) THEN
        ALTER TABLE policy
            ADD CONSTRAINT chk_policy_active_state
            CHECK (
                (is_active AND deactivated_at IS NULL)
                OR (NOT is_active AND deactivated_at IS NOT NULL)
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_policy_sync_job_started_at
    ON policy_sync_job (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_sync_job_status
    ON policy_sync_job (status, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_policy_last_seen_sync_job
    ON policy (last_seen_sync_job_id);

CREATE INDEX IF NOT EXISTS idx_policy_active_synced_at
    ON policy (synced_at DESC)
    WHERE is_active;

COMMIT;
