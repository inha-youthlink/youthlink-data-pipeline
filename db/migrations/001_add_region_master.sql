BEGIN;

CREATE TABLE IF NOT EXISTS region (
    region_code VARCHAR(5) PRIMARY KEY,

    sido_code VARCHAR(2) NOT NULL,
    sigungu_code VARCHAR(3) NOT NULL,

    sido_name VARCHAR(50) NOT NULL,
    sigungu_name VARCHAR(50) NOT NULL,
    region_name VARCHAR(100) NOT NULL,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    source VARCHAR(50) NOT NULL DEFAULT 'VWORLD',

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_region_code_length
        CHECK (LENGTH(region_code) = 5),

    CONSTRAINT chk_region_sido_code_length
        CHECK (LENGTH(sido_code) = 2),

    CONSTRAINT chk_region_sigungu_code_length
        CHECK (LENGTH(sigungu_code) = 3)
);

CREATE INDEX IF NOT EXISTS idx_region_sido_code
    ON region(sido_code);

CREATE INDEX IF NOT EXISTS idx_region_sido_name
    ON region(sido_name);

CREATE INDEX IF NOT EXISTS idx_region_sigungu_name
    ON region(sigungu_name);


CREATE TABLE IF NOT EXISTS region_code_alias (
    alias_code VARCHAR(5) PRIMARY KEY,

    canonical_code VARCHAR(5) NOT NULL
        REFERENCES region(region_code),

    valid_from DATE,
    valid_to DATE,

    reason TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_region_alias_code_length
        CHECK (LENGTH(alias_code) = 5),

    CONSTRAINT chk_region_alias_not_self
        CHECK (alias_code <> canonical_code)
);

CREATE INDEX IF NOT EXISTS idx_region_code_alias_canonical
    ON region_code_alias(canonical_code);

COMMIT;