-- The Youth Policy API can use 30000 to mean all of Daejeon rather than a
-- single current sigungu. Preserve that source code in policy_region and
-- expand it to every active Daejeon district through the existing 1:N parent
-- mapping contract.

BEGIN;

INSERT INTO public.region_code_mapping (
    source_code,
    canonical_code,
    mapping_type,
    valid_from,
    reason
)
VALUES
    (
        '30000', '30110', 'PARENT', NULL,
        '대전광역시 전체 범위 → 대전광역시 동구'
    ),
    (
        '30000', '30140', 'PARENT', NULL,
        '대전광역시 전체 범위 → 대전광역시 중구'
    ),
    (
        '30000', '30170', 'PARENT', NULL,
        '대전광역시 전체 범위 → 대전광역시 서구'
    ),
    (
        '30000', '30200', 'PARENT', NULL,
        '대전광역시 전체 범위 → 대전광역시 유성구'
    ),
    (
        '30000', '30230', 'PARENT', NULL,
        '대전광역시 전체 범위 → 대전광역시 대덕구'
    )
ON CONFLICT (source_code, canonical_code)
DO UPDATE SET
    mapping_type = EXCLUDED.mapping_type,
    valid_from = EXCLUDED.valid_from,
    valid_to = NULL,
    reason = EXCLUDED.reason;

COMMIT;
