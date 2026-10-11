BEGIN;

ALTER TABLE public.policy
    ADD COLUMN IF NOT EXISTS application_period_parse_status VARCHAR(20)
        NOT NULL DEFAULT 'EMPTY';

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.policy'::regclass
          AND conname = 'chk_policy_application_period_parse_status'
    ) THEN
        ALTER TABLE public.policy
            ADD CONSTRAINT chk_policy_application_period_parse_status
            CHECK (
                application_period_parse_status IN (
                    'EMPTY', 'SINGLE', 'MULTIPLE', 'RAW', 'INVALID'
                )
            );
    END IF;
END;
$migration$;

CREATE TABLE IF NOT EXISTS public.policy_application_period (
    policy_no       VARCHAR(30) NOT NULL,
    period_index    INTEGER NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,

    CONSTRAINT policy_application_period_pkey
        PRIMARY KEY (policy_no, period_index),

    CONSTRAINT fk_policy_application_period_policy
        FOREIGN KEY (policy_no)
        REFERENCES public.policy (policy_no)
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
    ON public.policy_application_period (start_date, end_date);

-- Preserve already trusted single ranges. The Python backfill command that
-- accompanies this migration reparses application_period_raw and replaces
-- these rows, including exact multi-period values separated by a literal \N.
INSERT INTO public.policy_application_period (
    policy_no,
    period_index,
    start_date,
    end_date
)
SELECT
    policy_no,
    0,
    application_start_date,
    application_end_date
FROM public.policy
WHERE application_start_date >= DATE '1900-01-01'
  AND application_end_date >= DATE '1900-01-01'
  AND application_start_date <= application_end_date
ON CONFLICT (policy_no, period_index) DO UPDATE
SET
    start_date = EXCLUDED.start_date,
    end_date = EXCLUDED.end_date;

-- Remove unsafe compatibility dates immediately. The raw API value remains in
-- application_period_raw and can be reviewed or reparsed without data loss.
UPDATE public.policy
SET
    application_start_date = NULL,
    application_end_date = NULL
WHERE application_start_date < DATE '1900-01-01'
   OR application_end_date < DATE '1900-01-01'
   OR application_start_date > application_end_date;

COMMIT;
