from __future__ import annotations
from pathlib import Path

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb
import requests
from dotenv import load_dotenv


load_dotenv()


# ============================================================
# Configuration
# ============================================================

API_URL = os.getenv("YOUTH_API_URL", "").strip()
API_KEY = os.getenv("YOUTH_API_KEY", "").strip()

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)

DEFAULT_PAGE_SIZE = int(os.getenv("YOUTH_API_PAGE_SIZE", "100"))


# ============================================================
# Helpers
# ============================================================


FAILED_LOG_PATH = Path("etl/logs/failed_policies.jsonl")


def log_failed_policy(
    item: dict[str, Any],
    error: Exception,
    page_num: int | None = None,
) -> None:
    FAILED_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "page_num": page_num,
        "policy_no": item.get("plcyNo"),
        "policy_name": item.get("plcyNm"),
        "error_type": type(error).__name__,
        "error_message": str(error),
        "raw_data": item,
    }

    with FAILED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                log_entry,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )


def clean_text(value: Any) -> str | None:
    """Trim API strings. Empty / whitespace-only values become None."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def split_csv(value: Any) -> list[str]:
    """Split comma-separated API fields such as zipCd / schoolCd."""
    text = clean_text(value)
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def yn_to_bool(value: Any) -> bool | None:
    text = clean_text(value)
    if text == "Y":
        return True
    if text == "N":
        return False
    return None


def to_int(value: Any) -> int | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def to_age(value: Any) -> int | None:
    age = to_int(value)

    if age is None:
        return None

    # API에서 0은 미설정 값으로 간주
    if age <= 0:
        return None
    return age


def normalize_age_range(min_value: Any, max_value: Any) -> tuple[int | None, int | None]:
    min_age = to_age(min_value)
    max_age = to_age(max_value)

    if min_age is not None and max_age is not None and min_age > max_age:
        return min_age, None

    return min_age, max_age


def parse_yyyymmdd(value: Any) -> date | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def parse_timestamp(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


_APPLICATION_RANGE_RE = re.compile(
    r"^\s*(\d{8})\s*~\s*(\d{8})\s*$"
)


def parse_application_period(value: Any) -> tuple[date | None, date | None, str | None]:
    """
    Example API value:
      20260223 ~ 20260313

    If the string cannot be parsed, dates remain NULL and the original
    value is preserved in application_period_raw.
    """
    raw = clean_text(value)
    if not raw:
        return None, None, None

    match = _APPLICATION_RANGE_RE.match(raw)
    if not match:
        return None, None, raw

    start = parse_yyyymmdd(match.group(1))
    end = parse_yyyymmdd(match.group(2))
    return start, end, raw


def paired_categories(large_raw: Any, middle_raw: Any) -> tuple[list[str], list[str]]:
    """
    We currently store lclsfNm and mclsfNm as parallel TEXT[] columns.
    Preserve each list in API order.
    """
    return split_csv(large_raw), split_csv(middle_raw)


# ============================================================
# API -> relational model transformation
# ============================================================

@dataclass
class PolicyRecord:
    policy: dict[str, Any]
    regions: list[str]
    eligibility_codes: list[tuple[str, str]]


def transform_policy(item: dict[str, Any]) -> PolicyRecord:
    policy_no = clean_text(item.get("plcyNo"))
    if not policy_no:
        raise ValueError("plcyNo is required")

    app_start, app_end, app_raw = parse_application_period(item.get("aplyYmd"))
    large_categories, middle_categories = paired_categories(
        item.get("lclsfNm"),
        item.get("mclsfNm"),
    )

    policy = {
        "policy_no": policy_no,
        "policy_name": clean_text(item.get("plcyNm")) or "(제목 없음)",
        "description": clean_text(item.get("plcyExplnCn")),
        "support_content": clean_text(item.get("plcySprtCn")),
        "keywords": split_csv(item.get("plcyKywdNm")),
        "large_categories": large_categories,
        "middle_categories": middle_categories,

        "provider_group_code": clean_text(item.get("pvsnInstGroupCd")),
        "provision_method_code": clean_text(item.get("plcyPvsnMthdCd")),
        "approval_status_code": clean_text(item.get("plcyAprvSttsCd")),

        "supervising_org_code": clean_text(item.get("sprvsnInstCd")),
        "supervising_org_name": clean_text(item.get("sprvsnInstCdNm")),
        "supervising_manager_name": clean_text(item.get("sprvsnInstPicNm")),

        "operating_org_code": clean_text(item.get("operInstCd")),
        "operating_org_name": clean_text(item.get("operInstCdNm")),
        "operating_manager_name": clean_text(item.get("operInstPicNm")),

        "support_scale_limit": yn_to_bool(item.get("sprtSclLmtYn")),
        "support_scale_count": to_int(item.get("sprtSclCnt")),
        "first_come_first_served": yn_to_bool(item.get("sprtArvlSeqYn")),

        "min_age": to_age(item.get("sprtTrgtMinAge")),
        "max_age": to_age(item.get("sprtTrgtMaxAge")),
        "age_limit_yn": yn_to_bool(item.get("sprtTrgtAgeLmtYn")),

        "marriage_status_code": clean_text(item.get("mrgSttsCd")),

        "income_condition_code": clean_text(item.get("earnCndSeCd")),
        "min_income": to_int(item.get("earnMinAmt")),
        "max_income": to_int(item.get("earnMaxAmt")),
        "income_etc": clean_text(item.get("earnEtcCn")),

        "application_period_type_code": clean_text(item.get("aplyPrdSeCd")),
        "application_start_date": app_start,
        "application_end_date": app_end,
        "application_period_raw": app_raw,

        "business_period_type_code": clean_text(item.get("bizPrdSeCd")),
        "business_start_date": parse_yyyymmdd(item.get("bizPrdBgngYmd")),
        "business_end_date": parse_yyyymmdd(item.get("bizPrdEndYmd")),
        "business_period_etc": clean_text(item.get("bizPrdEtcCn")),

        "application_method": clean_text(item.get("plcyAplyMthdCn")),
        "screening_method": clean_text(item.get("srngMthdCn")),
        "submission_documents": clean_text(item.get("sbmsnDcmntCn")),
        "additional_notes": clean_text(item.get("etcMttrCn")),
        "additional_qualification": clean_text(item.get("addAplyQlfcCndCn")),
        "participation_exclusion": clean_text(item.get("ptcpPrpTrgtCn")),

        "application_url": clean_text(item.get("aplyUrlAddr")),
        "reference_url_1": clean_text(item.get("refUrlAddr1")),
        "reference_url_2": clean_text(item.get("refUrlAddr2")),

        "registrar_org_code": clean_text(item.get("rgtrInstCd")),
        "registrar_org_name": clean_text(item.get("rgtrInstCdNm")),

        "source_created_at": parse_timestamp(item.get("frstRegDt")),
        "source_updated_at": parse_timestamp(item.get("lastMdfcnDt")),

        # Always preserve the full API item for reprocessing/debugging.
        "raw_data": Jsonb(item),
    }

    regions = split_csv(item.get("zipCd"))

    eligibility_codes: list[tuple[str, str]] = []
    eligibility_fields = {
        "MAJOR": "plcyMajorCd",
        "JOB": "jobCd",
        "SCHOOL": "schoolCd",
        "SPECIAL": "sbizCd",
    }

    for condition_type, field_name in eligibility_fields.items():
        for code in split_csv(item.get(field_name)):
            eligibility_codes.append((condition_type, code))

    return PolicyRecord(
        policy=policy,
        regions=regions,
        eligibility_codes=eligibility_codes,
    )


# ============================================================
# PostgreSQL loading
# ============================================================

POLICY_COLUMNS = [
    "policy_no",
    "policy_name",
    "description",
    "support_content",
    "keywords",
    "large_categories",
    "middle_categories",
    "provider_group_code",
    "provision_method_code",
    "approval_status_code",
    "supervising_org_code",
    "supervising_org_name",
    "supervising_manager_name",
    "operating_org_code",
    "operating_org_name",
    "operating_manager_name",
    "support_scale_limit",
    "support_scale_count",
    "first_come_first_served",
    "min_age",
    "max_age",
    "age_limit_yn",
    "marriage_status_code",
    "income_condition_code",
    "min_income",
    "max_income",
    "income_etc",
    "application_period_type_code",
    "application_start_date",
    "application_end_date",
    "application_period_raw",
    "business_period_type_code",
    "business_start_date",
    "business_end_date",
    "business_period_etc",
    "application_method",
    "screening_method",
    "submission_documents",
    "additional_notes",
    "additional_qualification",
    "participation_exclusion",
    "application_url",
    "reference_url_1",
    "reference_url_2",
    "registrar_org_code",
    "registrar_org_name",
    "source_created_at",
    "source_updated_at",
    "raw_data",
]


def build_policy_upsert_sql() -> str:
    column_sql = ", ".join(POLICY_COLUMNS)
    placeholders = ", ".join(f"%({column})s" for column in POLICY_COLUMNS)

    # policy_no is the API's natural key.
    update_columns = [c for c in POLICY_COLUMNS if c != "policy_no"]
    update_sql = ",\n        ".join(
        f"{column} = EXCLUDED.{column}" for column in update_columns
    )

    return f"""
    INSERT INTO policy (
        {column_sql}
    )
    VALUES (
        {placeholders}
    )
    ON CONFLICT (policy_no) DO UPDATE
    SET
        {update_sql},
        synced_at = NOW()
    """


POLICY_UPSERT_SQL = build_policy_upsert_sql()


def load_policy(cur: psycopg.Cursor, record: PolicyRecord) -> None:
    """
    Idempotent per-policy load:
      1) UPSERT policy
      2) replace regions
      3) replace eligibility-code rows
    """
    cur.execute(POLICY_UPSERT_SQL, record.policy)

    policy_no = record.policy["policy_no"]

    cur.execute(
        "DELETE FROM policy_region WHERE policy_no = %s",
        (policy_no,),
    )

    if record.regions:
        cur.executemany(
            """
            INSERT INTO policy_region (policy_no, region_code)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            [(policy_no, region) for region in record.regions],
        )

    cur.execute(
        "DELETE FROM policy_eligibility_code WHERE policy_no = %s",
        (policy_no,),
    )

    if record.eligibility_codes:
        cur.executemany(
            """
            INSERT INTO policy_eligibility_code (
                policy_no,
                condition_type,
                condition_code
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (policy_no, condition_type, condition_code)
            DO NOTHING
            """,
            [
                (policy_no, condition_type, condition_code)
                for condition_type, condition_code
                in record.eligibility_codes
            ],
        )


def load_items(items: Iterable[dict[str, Any]]) -> int:
    loaded = 0

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            for item in items:
                record = transform_policy(item)
                load_policy(cur, record)
                loaded += 1

        conn.commit()

    return loaded


# ============================================================
# 온통청년 API
# ============================================================

def fetch_page(page_num: int, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    if not API_URL:
        raise RuntimeError("YOUTH_API_URL is not set")
    if not API_KEY:
        raise RuntimeError("YOUTH_API_KEY is not set")

    params = {
        "apiKeyNm": API_KEY,
        "pageNum": page_num,
        "pageSize": page_size,
        "pageType": "1",
        "rtnType": "json",
    }

    response = requests.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def extract_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """
    Expected response shape from the supplied API sample:

      result
        ├─ pagging
        │    └─ totCount
        └─ youthPolicyList
    """
    result = payload.get("result") or {}
    items = result.get("youthPolicyList") or []
    paging = result.get("pagging") or {}
    total_count = to_int(paging.get("totCount")) or len(items)
    return items, total_count


def run_api_etl(page_size: int = DEFAULT_PAGE_SIZE) -> tuple[int, int]:
    success_count = 0
    failed_count = 0
    processed_count = 0
    page_num = 1

    with psycopg.connect(DB_DSN) as conn:

        while True:
            payload = fetch_page(page_num, page_size)
            items, total_count = extract_page(payload)

            if not items:
                break

            with conn.transaction():

                for item in items:
                    try:
                        with conn.transaction():
                            with conn.cursor() as cur:
                                record = transform_policy(item)
                                load_policy(cur, record)

                        success_count += 1

                    except Exception as e:
                        failed_count += 1
                        log_failed_policy(
                            item=item,
                            error=e,
                            page_num=page_num,
                        )

                        print(
                            f"[FAILED] "
                            f"page={page_num} "
                            f"policy_no={item.get('plcyNo')} "
                            f"name={item.get('plcyNm')} "
                            f"error={type(e).__name__}"
                        )

                    finally:
                        processed_count += 1

            print(
                f"[page {page_num}] "
                f"processed={len(items)} "
                f"success={success_count} "
                f"failed={failed_count} "
                f"processed_total={processed_count} "
                f"api_total={total_count}"
            )

            # 마지막 페이지 판별
            if len(items) < page_size:
                break

            page_num += 1

    return success_count, failed_count


# ============================================================
# Local sample mode
# ============================================================

def run_json_file(path: str) -> int:
    """
    Useful before calling the real API.

    Accepts either:
      - full API response JSON
      - a JSON array of policy objects
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, list):
        items = payload
    else:
        items, _ = extract_page(payload)

    loaded = load_items(items)
    print(f"loaded {loaded} policies")
    return loaded


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    api_parser = sub.add_parser("api", help="Load all pages from the Youth API")
    api_parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
    )

    file_parser = sub.add_parser("file", help="Load a local JSON response")
    file_parser.add_argument("path")

    args = parser.parse_args()

if args.command == "api":
    success_count, failed_count = run_api_etl(args.page_size)

    print()
    print("========== ETL RESULT ==========")
    print(f"success : {success_count}")
    print(f"failed  : {failed_count}")
    print(f"total   : {success_count + failed_count}")
    print("================================")

    if failed_count > 0:
        print(f"failed log: {FAILED_LOG_PATH}")
    elif args.command == "file":
        run_json_file(args.path)
