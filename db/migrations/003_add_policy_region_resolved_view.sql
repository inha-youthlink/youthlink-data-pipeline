BEGIN;

CREATE OR REPLACE VIEW v_policy_region_resolved AS

-- =========================================================
-- 1. 과거 코드 / 상위 코드를 현재 코드로 매핑
--    1:N 매핑인 경우 여러 행으로 확장됨
-- =========================================================
SELECT
    pr.policy_no,
    pr.region_code AS source_region_code,

    m.canonical_code AS resolved_region_code,

    r.sido_code,
    r.sigungu_code,
    r.sido_name,
    r.sigungu_name,
    r.region_name,
    r.is_active,

    'MAPPED'::VARCHAR(10) AS resolution_type,

    m.mapping_type,
    m.valid_from,
    m.valid_to

FROM policy_region pr

JOIN region_code_mapping m
    ON m.source_code = pr.region_code

JOIN region r
    ON r.region_code = m.canonical_code


UNION ALL


-- =========================================================
-- 2. 이미 현재 region 코드인 경우
-- =========================================================
SELECT
    pr.policy_no,
    pr.region_code AS source_region_code,

    pr.region_code AS resolved_region_code,

    r.sido_code,
    r.sigungu_code,
    r.sido_name,
    r.sigungu_name,
    r.region_name,
    r.is_active,

    'DIRECT'::VARCHAR(10) AS resolution_type,

    NULL::VARCHAR(20) AS mapping_type,
    NULL::DATE AS valid_from,
    NULL::DATE AS valid_to

FROM policy_region pr

JOIN region r
    ON r.region_code = pr.region_code

WHERE NOT EXISTS (
    SELECT 1
    FROM region_code_mapping m
    WHERE m.source_code = pr.region_code
)


UNION ALL


-- =========================================================
-- 3. region에도 없고 mapping에도 없는 미해결 코드
--    향후 API에 신규/이상 코드가 들어왔을 때 탐지용
-- =========================================================
SELECT
    pr.policy_no,
    pr.region_code AS source_region_code,

    NULL::VARCHAR(5) AS resolved_region_code,

    NULL::VARCHAR(2) AS sido_code,
    NULL::VARCHAR(3) AS sigungu_code,
    NULL::VARCHAR(50) AS sido_name,
    NULL::VARCHAR(50) AS sigungu_name,
    NULL::VARCHAR(100) AS region_name,
    NULL::BOOLEAN AS is_active,

    'UNRESOLVED'::VARCHAR(10) AS resolution_type,

    NULL::VARCHAR(20) AS mapping_type,
    NULL::DATE AS valid_from,
    NULL::DATE AS valid_to

FROM policy_region pr

WHERE NOT EXISTS (
    SELECT 1
    FROM region r
    WHERE r.region_code = pr.region_code
)
AND NOT EXISTS (
    SELECT 1
    FROM region_code_mapping m
    WHERE m.source_code = pr.region_code
);

COMMIT;