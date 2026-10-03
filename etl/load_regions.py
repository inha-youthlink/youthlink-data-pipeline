from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
import requests
from dotenv import load_dotenv


load_dotenv()


VWORLD_API_URL = "https://api.vworld.kr/req/data"
VWORLD_API_KEY = os.getenv("VWORLD_API_KEY")
VWORLD_API_DOMAIN = os.getenv("VWORLD_API_DOMAIN")

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)


BASE_PARAMS = {
    "key": VWORLD_API_KEY,
    "domain": VWORLD_API_DOMAIN,
    "service": "data",
    "version": "2.0",
    "request": "getfeature",
    "format": "json",
    "size": 1000,
    "page": 1,
    "geometry": "false",
    "attribute": "true",
    "crs": "EPSG:3857",
    "geomfilter": (
        "BOX("
        "13663271.680031825,"
        "3894007.9689600193,"
        "14817776.555251127,"
        "4688953.0631258525"
        ")"
    ),
}


@dataclass
class Region:
    region_code: str
    sido_code: str
    sigungu_code: str
    sido_name: str
    sigungu_name: str

    @property
    def region_name(self) -> str:
        return f"{self.sido_name} {self.sigungu_name}"


def request_vworld(params: dict) -> list[dict]:
    response = requests.get(
        VWORLD_API_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    status = data.get("response", {}).get("status")

    if status != "OK":
        raise RuntimeError(
            f"VWORLD API error: {data}"
        )

    return (
        data["response"]
        ["result"]
        ["featureCollection"]
        ["features"]
    )


def get_sidos() -> list[dict]:
    params = {
        **BASE_PARAMS,
        "data": "LT_C_ADSIDO_INFO",
    }

    return request_vworld(params)


def get_sigungu(sido_code: str) -> list[dict]:
    params = {
        **BASE_PARAMS,
        "data": "LT_C_ADSIGG_INFO",
        "attrfilter": f"sig_cd:like:{sido_code}",
    }

    return request_vworld(params)


def fetch_all_regions() -> list[Region]:
    region_by_code: dict[str, Region] = {}

    raw_feature_count = 0
    duplicate_count = 0

    sidos = get_sidos()

    print(f"[VWORLD] sido count: {len(sidos)}")

    for sido_feature in sidos:
        props = sido_feature["properties"]

        sido_code = str(
            props.get("ctprvn_cd", "")
        ).strip()

        sido_name = str(
            props.get("ctp_kor_nm", "")
        ).strip()

        if not sido_code or not sido_name:
            print(
                "[WARN] invalid sido:",
                props,
            )
            continue

        sigungu_list = get_sigungu(sido_code)

        matched_count = sum(
            1
            for feature in sigungu_list
            if str(
                feature.get("properties", {}).get("sig_cd", "")
            ).strip().startswith(sido_code)
        )

        print(
            f"[VWORLD] "
            f"{sido_code} {sido_name}: "
            f"{matched_count} matched "
            f"({len(sigungu_list)} raw)"
        )

        for sigungu_feature in sigungu_list:
            sigungu_props = sigungu_feature["properties"]

            region_code = str(
                sigungu_props.get("sig_cd", "")
            ).strip()

            sigungu_name = str(
                sigungu_props.get("sig_kor_nm", "")
            ).strip()

            # VWorld의 like 필터가 prefix가 아닌 부분 문자열까지
            # 반환할 수 있으므로 시도 코드 앞 2자리를 직접 검증한다.
            if not region_code.startswith(sido_code):
                continue

            if len(region_code) != 5:
                print(
                    "[WARN] invalid region code:",
                    region_code,
                    sigungu_name,
                )
                continue

            if not sigungu_name:
                print(
                    "[WARN] empty sigungu name:",
                    region_code,
                )
                continue

            raw_feature_count += 1

            region = Region(
                region_code=region_code,
                sido_code=region_code[:2],
                sigungu_code=region_code[2:],
                sido_name=sido_name,
                sigungu_name=sigungu_name,
            )

            existing = region_by_code.get(region_code)

            if existing is None:
                region_by_code[region_code] = region

            else:
                duplicate_count += 1

                if (
                    existing.sido_name != region.sido_name
                    or existing.sigungu_name != region.sigungu_name
                ):
                    raise RuntimeError(
                        "Conflicting region data detected: "
                        f"{region_code} "
                        f"{existing.sido_name} {existing.sigungu_name} "
                        f"!= "
                        f"{region.sido_name} {region.sigungu_name}"
                    )

    regions = list(region_by_code.values())

    print()
    print(f"[VWORLD] raw features      : {raw_feature_count}")
    print(f"[VWORLD] duplicate features: {duplicate_count}")
    print(f"[VWORLD] unique regions    : {len(regions)}")

    return regions

UPSERT_SQL = """
INSERT INTO region (
    region_code,
    sido_code,
    sigungu_code,
    sido_name,
    sigungu_name,
    region_name,
    is_active,
    source,
    updated_at
)
VALUES (
    %(region_code)s,
    %(sido_code)s,
    %(sigungu_code)s,
    %(sido_name)s,
    %(sigungu_name)s,
    %(region_name)s,
    TRUE,
    'VWORLD',
    NOW()
)
ON CONFLICT (region_code)
DO UPDATE SET
    sido_code = EXCLUDED.sido_code,
    sigungu_code = EXCLUDED.sigungu_code,
    sido_name = EXCLUDED.sido_name,
    sigungu_name = EXCLUDED.sigungu_name,
    region_name = EXCLUDED.region_name,
    is_active = TRUE,
    source = EXCLUDED.source,
    updated_at = NOW();
"""


def validate_regions(regions: list[Region]) -> None:
    if not regions:
        raise RuntimeError(
            "VWORLD returned no region data."
        )

    codes = [
        region.region_code
        for region in regions
    ]

    if len(codes) != len(set(codes)):
        raise RuntimeError(
            "Duplicate region codes detected."
        )

    sido_codes = {
        region.sido_code
        for region in regions
    }

    if len(sido_codes) < 16:
        raise RuntimeError(
            "VWORLD response looks incomplete: "
            f"only {len(sido_codes)} sido codes returned."
        )

    # 현재 정상 수치보다 지나치게 적으면
    # 기존 region 데이터를 inactive 처리하지 않도록 중단
    if len(regions) < 200:
        raise RuntimeError(
            "VWORLD response looks incomplete: "
            f"only {len(regions)} regions returned."
        )

    for region in regions:
        if len(region.region_code) != 5:
            raise RuntimeError(
                f"Invalid region code: "
                f"{region.region_code}"
            )

        if region.region_code[:2] != region.sido_code:
            raise RuntimeError(
                f"Invalid sido code: "
                f"{region.region_code}"
            )

        if region.region_code[2:] != region.sigungu_code:
            raise RuntimeError(
                f"Invalid sigungu code: "
                f"{region.region_code}"
            )


def save_regions(regions: list[Region]) -> None:
    with psycopg.connect(DB_DSN) as conn:
        with conn.transaction():
            with conn.cursor() as cur:

                # 이전에는 존재했지만 현재 VWORLD에서
                # 조회되지 않는 지역을 inactive 처리
                cur.execute(
                    """
                    UPDATE region
                    SET
                        is_active = FALSE,
                        updated_at = NOW()
                    WHERE source = 'VWORLD'
                    """
                )

                for region in regions:
                    cur.execute(
                        UPSERT_SQL,
                        {
                            "region_code": region.region_code,
                            "sido_code": region.sido_code,
                            "sigungu_code": region.sigungu_code,
                            "sido_name": region.sido_name,
                            "sigungu_name": region.sigungu_name,
                            "region_name": region.region_name,
                        },
                    )

def validate_policy_region_resolution(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT pr.region_code)
            FROM policy_region pr

            LEFT JOIN region r
                ON r.region_code = pr.region_code

            LEFT JOIN region_code_mapping m
                ON m.source_code = pr.region_code

            WHERE r.region_code IS NULL
              AND m.source_code IS NULL
            """
        )

        unresolved_count = cur.fetchone()[0]

    return unresolved_count

def check_unresolved_regions() -> list[str]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT pr.region_code
                FROM policy_region pr

                LEFT JOIN region r
                    ON r.region_code = pr.region_code

                LEFT JOIN region_code_mapping m
                    ON m.source_code = pr.region_code

                WHERE r.region_code IS NULL
                  AND m.source_code IS NULL

                ORDER BY pr.region_code
                """
            )

            return [
                row[0]
                for row in cur.fetchall()
            ]

def main() -> None:
    print("=== VWORLD region sync start ===")

    regions = fetch_all_regions()

    print()
    print(f"fetched regions: {len(regions)}")

    validate_regions(regions)

    print("validation: OK")

    save_regions(regions)

    print("database sync: OK")

    unresolved = check_unresolved_regions()

    print()
    print("=== policy region validation ===")

    if unresolved:
        print(
            f"[WARN] unresolved region codes: "
            f"{len(unresolved)}"
        )

        for code in unresolved:
            print(f"  - {code}")

    else:
        print("unresolved region codes: 0")
        print("all policy regions resolved: OK")

    print()
    print("=== VWORLD region sync complete ===")


if __name__ == "__main__":
    main()