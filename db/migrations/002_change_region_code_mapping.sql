BEGIN;

CREATE TABLE IF NOT EXISTS public.region_code_mapping (
    source_code VARCHAR(5) NOT NULL,

    canonical_code VARCHAR(5) NOT NULL
        REFERENCES public.region(region_code),

    mapping_type VARCHAR(20) NOT NULL DEFAULT 'REPLACED',

    valid_from DATE,
    valid_to DATE,

    reason TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT region_code_mapping_pkey
        PRIMARY KEY (source_code, canonical_code),

    CONSTRAINT chk_region_code_mapping_source_length
        CHECK (LENGTH(source_code) = 5),

    CONSTRAINT chk_region_code_mapping_not_self
        CHECK (source_code <> canonical_code),

    CONSTRAINT chk_region_code_mapping_type
        CHECK (
            mapping_type IN (
                'REPLACED',
                'SPLIT',
                'MERGED',
                'PARENT'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_region_code_mapping_canonical
    ON public.region_code_mapping(canonical_code);

COMMIT;

--REPLACED
--옛 코드가 새로운 코드 하나로 대체

--SPLIT
--옛 지역 하나가 여러 지역으로 분리

--MERGED
--여러 옛 지역이 하나로 통합

--PARENT
--온통청년 코드가 더 넓은 상위 지역을 의미
--예: 천안시 전체 → 동남구 + 서북구