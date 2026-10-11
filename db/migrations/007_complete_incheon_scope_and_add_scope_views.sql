BEGIN;

-- Official references for the 2026-07-01 Incheon reorganization:
-- - Ministry of the Interior and Safety code release:
--   https://www.mois.go.kr/frt/bbs/type001/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000052&nttId=127039
-- - Incheon Metropolitan City overview:
--   https://www.incheon.go.kr/IC01070101
--
-- Some Youth Policy API records already replaced the old Jung-gu/Seo-gu codes
-- with only one successor each. When, and only when, a policy has exactly the
-- nine Incheon codes below, complete that city-wide set with Yeongjong-gu and
-- Seohae-gu. policy_region remains unchanged as the raw source record.
CREATE OR REPLACE VIEW public.v_policy_region_resolved AS
WITH incheon_scope_candidates AS (
    SELECT pr.policy_no
    FROM public.policy_region AS pr
    WHERE pr.region_code LIKE '28%'
    GROUP BY pr.policy_no
    HAVING ARRAY_AGG(DISTINCT pr.region_code ORDER BY pr.region_code)
        = ARRAY[
            '28125', '28177', '28185', '28200', '28237',
            '28245', '28290', '28710', '28720'
        ]::VARCHAR[]
),
incheon_scope_completion AS (
    SELECT
        candidate.policy_no,
        inferred.resolved_region_code
    FROM incheon_scope_candidates AS candidate
    CROSS JOIN (
        VALUES ('28155'::VARCHAR(5)), ('28275'::VARCHAR(5))
    ) AS inferred(resolved_region_code)
)

-- 1. Historical or parent code expanded through the mapping table.
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
FROM public.policy_region AS pr
JOIN public.region_code_mapping AS m
  ON m.source_code = pr.region_code
JOIN public.region AS r
  ON r.region_code = m.canonical_code

UNION ALL

-- 2. Source code already matches the current region master.
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
FROM public.policy_region AS pr
JOIN public.region AS r
  ON r.region_code = pr.region_code
WHERE NOT EXISTS (
    SELECT 1
    FROM public.region_code_mapping AS m
    WHERE m.source_code = pr.region_code
)

UNION ALL

-- 3. Complete the known nine-code Incheon scope anomaly. There is no single
-- original code for an inferred row, so source_region_code is intentionally
-- NULL rather than pretending that a different source code produced it.
SELECT
    completion.policy_no,
    NULL::VARCHAR(20) AS source_region_code,
    completion.resolved_region_code,
    r.sido_code,
    r.sigungu_code,
    r.sido_name,
    r.sigungu_name,
    r.region_name,
    r.is_active,
    'INFERRED'::VARCHAR(10) AS resolution_type,
    'SCOPE_COMPLETION'::VARCHAR(20) AS mapping_type,
    DATE '2026-07-01' AS valid_from,
    NULL::DATE AS valid_to
FROM incheon_scope_completion AS completion
JOIN public.region AS r
  ON r.region_code = completion.resolved_region_code

UNION ALL

-- 4. Code cannot be resolved by either the current master or a mapping.
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
FROM public.policy_region AS pr
WHERE NOT EXISTS (
    SELECT 1
    FROM public.region AS r
    WHERE r.region_code = pr.region_code
)
AND NOT EXISTS (
    SELECT 1
    FROM public.region_code_mapping AS m
    WHERE m.source_code = pr.region_code
);


-- One row per policy. This is the shared nationwide/partial/unknown contract
-- for the backend and RAG instead of duplicating set-comparison logic.
CREATE OR REPLACE VIEW public.v_policy_region_scope AS
WITH active_region_total AS (
    SELECT COUNT(*)::INTEGER AS region_count
    FROM public.region
    WHERE is_active = TRUE
),
source_stats AS (
    SELECT policy_no, COUNT(*)::INTEGER AS source_region_count
    FROM public.policy_region
    GROUP BY policy_no
),
resolved_stats AS (
    SELECT
        policy_no,
        COUNT(DISTINCT resolved_region_code) FILTER (
            WHERE is_active = TRUE
        )::INTEGER AS resolved_active_region_count,
        COUNT(*) FILTER (
            WHERE resolution_type = 'UNRESOLVED'
        )::INTEGER AS unresolved_region_count,
        COUNT(*) FILTER (
            WHERE is_active = FALSE
        )::INTEGER AS inactive_region_count
    FROM public.v_policy_region_resolved
    GROUP BY policy_no
)
SELECT
    p.policy_no,
    CASE
        WHEN total.region_count = 0 THEN 'UNKNOWN'
        WHEN COALESCE(source.source_region_count, 0) = 0 THEN 'UNKNOWN'
        WHEN COALESCE(resolved.unresolved_region_count, 0) > 0 THEN 'UNKNOWN'
        WHEN COALESCE(resolved.inactive_region_count, 0) > 0 THEN 'UNKNOWN'
        WHEN COALESCE(resolved.resolved_active_region_count, 0)
             = total.region_count THEN 'NATIONWIDE'
        ELSE 'PARTIAL'
    END::VARCHAR(10) AS region_scope,
    COALESCE(source.source_region_count, 0) AS source_region_count,
    COALESCE(resolved.resolved_active_region_count, 0)
        AS resolved_active_region_count,
    total.region_count AS total_active_region_count,
    COALESCE(resolved.unresolved_region_count, 0) AS unresolved_region_count,
    COALESCE(resolved.inactive_region_count, 0) AS inactive_region_count
FROM public.policy AS p
CROSS JOIN active_region_total AS total
LEFT JOIN source_stats AS source
  ON source.policy_no = p.policy_no
LEFT JOIN resolved_stats AS resolved
  ON resolved.policy_no = p.policy_no;


-- One row per policy and selected sido. A full sido set is explicitly marked
-- so UI/RAG code can render "{sido_name} 전체" consistently.
CREATE OR REPLACE VIEW public.v_policy_sido_scope AS
WITH active_sido_total AS (
    SELECT
        sido_code,
        MIN(sido_name) AS sido_name,
        COUNT(*)::INTEGER AS total_active_region_count
    FROM public.region
    WHERE is_active = TRUE
    GROUP BY sido_code
),
selected AS (
    SELECT
        policy_no,
        sido_code,
        COUNT(DISTINCT resolved_region_code)::INTEGER
            AS resolved_active_region_count
    FROM public.v_policy_region_resolved
    WHERE is_active = TRUE
      AND resolution_type <> 'UNRESOLVED'
    GROUP BY policy_no, sido_code
)
SELECT
    selected.policy_no,
    selected.sido_code,
    total.sido_name,
    CASE
        WHEN selected.resolved_active_region_count
             = total.total_active_region_count THEN 'SIDO_WIDE'
        ELSE 'PARTIAL'
    END::VARCHAR(10) AS sido_scope,
    selected.resolved_active_region_count,
    total.total_active_region_count
FROM selected
JOIN active_sido_total AS total
  ON total.sido_code = selected.sido_code;

COMMENT ON VIEW public.v_policy_region_scope IS
    'One-row-per-policy region scope shared by backend and RAG';
COMMENT ON VIEW public.v_policy_sido_scope IS
    'Per-policy, per-sido coverage shared by backend and RAG';

COMMIT;
