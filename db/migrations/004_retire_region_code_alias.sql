BEGIN;

DO $migration$
DECLARE
    conflicting_alias RECORD;
BEGIN
    -- This migration is safe to re-run after region_code_alias has been removed.
    IF to_regclass('public.region_code_alias') IS NULL THEN
        RETURN;
    END IF;

    -- 002_change_region_code_mapping.sql must be applied first. Raising here keeps
    -- the legacy table and its data intact when migrations are run out of order.
    IF to_regclass('public.region_code_mapping') IS NULL THEN
        RAISE EXCEPTION
            'public.region_code_mapping does not exist; apply migration 002 before migration 004';
    END IF;

    -- Prevent rows from being added between the copy and the table removal.
    LOCK TABLE public.region_code_alias IN ACCESS EXCLUSIVE MODE;

    -- The legacy schema normally prevents self mappings. Check explicitly so a
    -- database whose old constraint was removed cannot silently lose such rows.
    SELECT
        alias_code,
        canonical_code
    INTO conflicting_alias
    FROM public.region_code_alias
    WHERE alias_code = canonical_code
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'cannot migrate self mapping %.% from public.region_code_alias',
            conflicting_alias.alias_code,
            conflicting_alias.canonical_code;
    END IF;

    -- A pre-existing mapping may safely contribute metadata that the alias row
    -- does not have. If both rows contain different non-null values, stop instead
    -- of choosing one value and dropping the other with the legacy table.
    SELECT
        alias_row.alias_code,
        alias_row.canonical_code
    INTO conflicting_alias
    FROM public.region_code_alias AS alias_row
    JOIN public.region_code_mapping AS mapping_row
      ON mapping_row.source_code = alias_row.alias_code
     AND mapping_row.canonical_code = alias_row.canonical_code
    WHERE (
            alias_row.valid_from IS NOT NULL
        AND mapping_row.valid_from IS NOT NULL
        AND alias_row.valid_from IS DISTINCT FROM mapping_row.valid_from
    )
       OR (
            alias_row.valid_to IS NOT NULL
        AND mapping_row.valid_to IS NOT NULL
        AND alias_row.valid_to IS DISTINCT FROM mapping_row.valid_to
    )
       OR (
            alias_row.reason IS NOT NULL
        AND mapping_row.reason IS NOT NULL
        AND alias_row.reason IS DISTINCT FROM mapping_row.reason
    )
    LIMIT 1;

    IF FOUND THEN
        RAISE EXCEPTION
            'conflicting metadata for region mapping %.%; public.region_code_alias was not removed',
            conflicting_alias.alias_code,
            conflicting_alias.canonical_code;
    END IF;

    INSERT INTO public.region_code_mapping (
        source_code,
        canonical_code,
        mapping_type,
        valid_from,
        valid_to,
        reason,
        created_at
    )
    SELECT
        alias_code,
        canonical_code,
        'REPLACED',
        valid_from,
        valid_to,
        reason,
        created_at
    FROM public.region_code_alias
    ON CONFLICT (source_code, canonical_code) DO UPDATE
    SET
        -- Keep an existing, more specific mapping_type (for example SPLIT or
        -- PARENT), fill missing legacy metadata, and retain the earliest record
        -- creation time for the same source/canonical relationship.
        valid_from = COALESCE(
            public.region_code_mapping.valid_from,
            EXCLUDED.valid_from
        ),
        valid_to = COALESCE(
            public.region_code_mapping.valid_to,
            EXCLUDED.valid_to
        ),
        reason = COALESCE(
            public.region_code_mapping.reason,
            EXCLUDED.reason
        ),
        created_at = LEAST(
            public.region_code_mapping.created_at,
            EXCLUDED.created_at
        );
END;
$migration$;

DROP TABLE IF EXISTS public.region_code_alias;

COMMIT;
