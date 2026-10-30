from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

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
DEFAULT_REQUEST_DELAY_SECONDS = float(
    os.getenv("YOUTH_API_REQUEST_DELAY_SECONDS", "1")
)
DEFAULT_MAX_ATTEMPTS = int(os.getenv("YOUTH_API_MAX_ATTEMPTS", "3"))
DEFAULT_RETRY_BASE_DELAY_SECONDS = float(
    os.getenv("YOUTH_API_RETRY_BASE_DELAY_SECONDS", "1")
)
MAX_RETRY_DELAY_SECONDS = 30.0


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


_APPLICATION_RANGE_RE = re.compile(r"^\s*(\d{8})\s*~\s*(\d{8})\s*$")
_APPLICATION_PERIOD_SEPARATOR_RE = re.compile(r"(?:\\N|\r\n|\r|\n)+")
_MIN_APPLICATION_DATE = date(1900, 1, 1)


@dataclass(frozen=True)
class ParsedApplicationPeriod:
    raw: str | None
    periods: tuple[tuple[date, date], ...]
    status: str

    @property
    def legacy_start_date(self) -> date | None:
        if len(self.periods) == 1:
            return self.periods[0][0]
        return None

    @property
    def legacy_end_date(self) -> date | None:
        if len(self.periods) == 1:
            return self.periods[0][1]
        return None


def parse_application_period(value: Any) -> ParsedApplicationPeriod:
    """
    Parse one or more application periods separated by a literal ``\\N`` or
    an actual newline.

    The legacy columns on ``policy`` are populated only when there is exactly
    one trusted period. Multiple periods are stored in
    ``policy_application_period`` so gaps between periods are not presented as
    continuously open. Invalid, reversed, or pre-1900 dates keep only the raw
    value and never make the policy load fail.
    """
    raw = clean_text(value)
    if not raw:
        return ParsedApplicationPeriod(raw=None, periods=(), status="EMPTY")

    segments = [
        segment.strip()
        for segment in _APPLICATION_PERIOD_SEPARATOR_RE.split(raw)
        if segment.strip()
    ]
    periods: list[tuple[date, date]] = []

    for segment in segments:
        match = _APPLICATION_RANGE_RE.fullmatch(segment)
        if not match:
            return ParsedApplicationPeriod(raw=raw, periods=(), status="RAW")

        start = parse_yyyymmdd(match.group(1))
        end = parse_yyyymmdd(match.group(2))
        if (
            start is None
            or end is None
            or start < _MIN_APPLICATION_DATE
            or end < _MIN_APPLICATION_DATE
            or start > end
        ):
            return ParsedApplicationPeriod(raw=raw, periods=(), status="INVALID")

        periods.append((start, end))

    return ParsedApplicationPeriod(
        raw=raw,
        periods=tuple(periods),
        status="SINGLE" if len(periods) == 1 else "MULTIPLE",
    )


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
    application_periods: tuple[tuple[date, date], ...]


def transform_policy(item: dict[str, Any]) -> PolicyRecord:
    policy_no = clean_text(item.get("plcyNo"))
    if not policy_no:
        raise ValueError("plcyNo is required")

    application_period = parse_application_period(item.get("aplyYmd"))
    large_categories, middle_categories = paired_categories(
        item.get("lclsfNm"),
        item.get("mclsfNm"),
    )
    min_age, max_age = normalize_age_range(
        item.get("sprtTrgtMinAge"),
        item.get("sprtTrgtMaxAge"),
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

        "min_age": min_age,
        "max_age": max_age,
        "age_limit_yn": yn_to_bool(item.get("sprtTrgtAgeLmtYn")),

        "marriage_status_code": clean_text(item.get("mrgSttsCd")),

        "income_condition_code": clean_text(item.get("earnCndSeCd")),
        "min_income": to_int(item.get("earnMinAmt")),
        "max_income": to_int(item.get("earnMaxAmt")),
        "income_etc": clean_text(item.get("earnEtcCn")),

        "application_period_type_code": clean_text(item.get("aplyPrdSeCd")),
        "application_start_date": application_period.legacy_start_date,
        "application_end_date": application_period.legacy_end_date,
        "application_period_raw": application_period.raw,
        "application_period_parse_status": application_period.status,

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
        application_periods=application_period.periods,
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
    "application_period_parse_status",
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


def load_policy(
    cur: psycopg.Cursor,
    record: PolicyRecord,
    sync_job_id: UUID | None = None,
) -> None:
    """
    Idempotent per-policy load:
      1) UPSERT policy
      2) replace application periods
      3) replace regions
      4) replace eligibility-code rows
    """
    cur.execute(POLICY_UPSERT_SQL, record.policy)

    policy_no = record.policy["policy_no"]

    # Only a full Youth API sync owns lifecycle state. Local JSON imports and
    # period backfills must not accidentally reactivate/deactivate policies.
    if sync_job_id is not None:
        cur.execute(
            """
            UPDATE policy
            SET is_active = TRUE,
                deactivated_at = NULL,
                last_seen_sync_job_id = %s
            WHERE policy_no = %s
            """,
            (sync_job_id, policy_no),
        )

    cur.execute(
        "DELETE FROM policy_application_period WHERE policy_no = %s",
        (policy_no,),
    )

    if record.application_periods:
        cur.executemany(
            """
            INSERT INTO policy_application_period (
                policy_no,
                period_index,
                start_date,
                end_date
            )
            VALUES (%s, %s, %s, %s)
            """,
            [
                (policy_no, index, start_date, end_date)
                for index, (start_date, end_date)
                in enumerate(record.application_periods)
            ],
        )

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


def backfill_application_periods() -> tuple[int, int, int]:
    """Re-parse stored API values after migration 006 without calling the API."""
    processed = 0
    parsed = 0
    invalid_or_empty = 0

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT policy_no, application_period_raw
                FROM policy
                ORDER BY policy_no
                """
            )
            rows = cur.fetchall()

            for policy_no, raw in rows:
                result = parse_application_period(raw)
                cur.execute(
                    """
                    UPDATE policy
                    SET application_start_date = %s,
                        application_end_date = %s,
                        application_period_parse_status = %s
                    WHERE policy_no = %s
                    """,
                    (
                        result.legacy_start_date,
                        result.legacy_end_date,
                        result.status,
                        policy_no,
                    ),
                )
                cur.execute(
                    "DELETE FROM policy_application_period WHERE policy_no = %s",
                    (policy_no,),
                )
                if result.periods:
                    cur.executemany(
                        """
                        INSERT INTO policy_application_period (
                            policy_no,
                            period_index,
                            start_date,
                            end_date
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        [
                            (policy_no, index, start_date, end_date)
                            for index, (start_date, end_date)
                            in enumerate(result.periods)
                        ],
                    )
                    parsed += 1
                else:
                    invalid_or_empty += 1
                processed += 1

        conn.commit()

    return processed, parsed, invalid_or_empty


# ============================================================
# 온통청년 API
# ============================================================

def validate_page_size(page_size: int) -> int:
    if page_size <= 0:
        raise ValueError("page_size must be a positive integer")
    return page_size


def validate_non_negative_delay(delay_seconds: float, name: str) -> float:
    if delay_seconds < 0:
        raise ValueError(f"{name} must not be negative")
    return delay_seconds


def validate_max_attempts(max_attempts: int) -> int:
    if max_attempts <= 0:
        raise ValueError("max attempts must be a positive integer")
    return max_attempts


def retry_delay_seconds(
    retry_after: str | None,
    attempt: int,
    base_delay_seconds: float,
) -> float:
    """Return a bounded retry delay, preferring a valid Retry-After value."""
    delay: float | None = None

    if retry_after:
        try:
            delay = float(retry_after.strip())
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (
                    retry_at - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = None

    if delay is None:
        delay = base_delay_seconds * (2 ** max(attempt - 1, 0))

    return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))


def wait_before_next_page(
    delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
) -> None:
    validate_non_negative_delay(delay_seconds, "request delay")
    if delay_seconds:
        time.sleep(delay_seconds)


def fetch_page(
    page_num: int,
    page_size: int = DEFAULT_PAGE_SIZE,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
) -> dict[str, Any]:
    validate_page_size(page_size)
    validate_max_attempts(max_attempts)
    validate_non_negative_delay(retry_base_delay_seconds, "retry base delay")

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

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(API_URL, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if attempt < max_attempts:
                time.sleep(
                    retry_delay_seconds(
                        None,
                        attempt,
                        retry_base_delay_seconds,
                    )
                )
                continue
            raise RuntimeError(
                f"Youth API page {page_num} failed after "
                f"{max_attempts} attempts: network error"
            ) from None
        except requests.RequestException:
            # Requests exceptions can include the prepared URL and therefore
            # the apiKeyNm query parameter. Never propagate the original text.
            raise RuntimeError(
                f"Youth API page {page_num} failed: request error"
            ) from None

        status_code = response.status_code
        if 200 <= status_code < 300:
            try:
                payload = response.json()
            except (ValueError, requests.RequestException):
                # JSON decoder errors may also retain response/request details.
                raise RuntimeError(
                    f"Youth API page {page_num} returned invalid JSON"
                ) from None
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Youth API page {page_num} returned invalid JSON"
                ) from None
            return payload

        retryable_status = status_code == 429 or 500 <= status_code <= 599
        if retryable_status and attempt < max_attempts:
            time.sleep(
                retry_delay_seconds(
                    response.headers.get("Retry-After"),
                    attempt,
                    retry_base_delay_seconds,
                )
            )
            continue

        # 403 and all other non-retryable statuses fail immediately. Do not
        # use response.raise_for_status(): its exception includes the full URL.
        raise RuntimeError(
            f"Youth API page {page_num} failed with HTTP {status_code}"
        ) from None

    raise AssertionError("unreachable")


def extract_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """
    Expected response shape from the supplied API sample:

      result
        ├─ pagging
        │    └─ totCount
        └─ youthPolicyList
    """
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")

    if "result" not in payload:
        raise ValueError("API response is missing 'result'")

    result = payload["result"]
    if not isinstance(result, dict):
        raise ValueError("API response 'result' must be an object")

    if "youthPolicyList" not in result:
        raise ValueError("API response is missing 'result.youthPolicyList'")

    items = result["youthPolicyList"]
    if not isinstance(items, list):
        raise ValueError("API response 'result.youthPolicyList' must be a list")
    if any(not isinstance(item, dict) for item in items):
        raise ValueError(
            "API response 'result.youthPolicyList' must contain only objects"
        )

    paging = result.get("pagging", {})
    if paging is None:
        paging = {}
    if not isinstance(paging, dict):
        raise ValueError("API response 'result.pagging' must be an object")

    total_count = to_int(paging.get("totCount")) or len(items)
    return items, total_count


def create_sync_job(conn: psycopg.Connection, page_size: int) -> UUID:
    """Persist the job before fetching pages so failures remain observable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_sync_job (
                source,
                status,
                page_size
            )
            VALUES ('YOUTH_API', 'RUNNING', %s)
            RETURNING sync_job_id
            """,
            (page_size,),
        )
        row = cur.fetchone()
    conn.commit()

    if row is None:
        raise RuntimeError("failed to create policy sync job")
    return row[0]


def evaluate_full_sync(
    *,
    full_scan_completed: bool,
    api_total_count: int | None,
    totals_consistent: bool,
    processed_count: int,
    success_count: int,
    failed_count: int,
    unique_success_count: int,
) -> tuple[bool, str | None]:
    """
    Decide whether it is safe to mark unseen policies inactive.

    This intentionally prefers leaving stale rows active over deactivating a
    valid policy after a truncated, duplicated, empty, or partially failed API
    response.
    """
    reasons: list[str] = []

    if not full_scan_completed:
        reasons.append("API pagination did not reach a normal end")
    if api_total_count is None or api_total_count <= 0:
        reasons.append("API total count is missing or empty")
    if not totals_consistent:
        reasons.append("API total count changed between pages")
    if failed_count:
        reasons.append(f"{failed_count} policies failed to load")
    if api_total_count is not None and processed_count != api_total_count:
        reasons.append(
            f"processed count {processed_count} != API total {api_total_count}"
        )
    if api_total_count is not None and success_count != api_total_count:
        reasons.append(
            f"success count {success_count} != API total {api_total_count}"
        )
    if unique_success_count != success_count:
        reasons.append(
            f"unique policy count {unique_success_count} != success count "
            f"{success_count}"
        )

    return not reasons, "; ".join(reasons) or None


def finish_sync_job(
    conn: psycopg.Connection,
    sync_job_id: UUID,
    *,
    status: str,
    api_total_count: int | None,
    processed_count: int,
    success_count: int,
    failed_count: int,
    deactivated_count: int,
    full_scan_completed: bool,
    deactivation_applied: bool,
    error_message: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE policy_sync_job
            SET status = %s,
                api_total_count = %s,
                processed_count = %s,
                success_count = %s,
                failed_count = %s,
                deactivated_count = %s,
                full_scan_completed = %s,
                deactivation_applied = %s,
                finished_at = NOW(),
                error_message = %s
            WHERE sync_job_id = %s
            """,
            (
                status,
                api_total_count,
                processed_count,
                success_count,
                failed_count,
                deactivated_count,
                full_scan_completed,
                deactivation_applied,
                error_message,
                sync_job_id,
            ),
        )


def deactivate_unseen_policies(
    cur: psycopg.Cursor,
    sync_job_id: UUID,
) -> int:
    """Soft-delete policies not observed by a verified complete API sync."""
    cur.execute(
        """
        UPDATE policy
        SET is_active = FALSE,
            deactivated_at = COALESCE(deactivated_at, NOW())
        WHERE is_active = TRUE
          AND last_seen_sync_job_id IS DISTINCT FROM %s
        """,
        (sync_job_id,),
    )
    return cur.rowcount


def run_api_etl(page_size: int = DEFAULT_PAGE_SIZE) -> tuple[int, int]:
    validate_page_size(page_size)

    success_count = 0
    failed_count = 0
    processed_count = 0
    page_num = 1
    api_total_count: int | None = None
    totals_consistent = True
    full_scan_completed = False
    successful_policy_nos: set[str] = set()

    with psycopg.connect(DB_DSN) as conn:
        sync_job_id = create_sync_job(conn, page_size)

        try:
            while True:
                payload = fetch_page(page_num, page_size)
                items, total_count = extract_page(payload)

                # An empty page is a normal terminator only after it has been
                # validated by the completion guard below. Ignore its fallback
                # total_count=0 when a prior non-empty page reported a total.
                if items:
                    if api_total_count is None:
                        api_total_count = total_count
                    elif total_count != api_total_count:
                        totals_consistent = False
                elif api_total_count is None:
                    api_total_count = total_count

                if not items:
                    full_scan_completed = True
                    break

                with conn.transaction():
                    for item in items:
                        try:
                            with conn.transaction():
                                with conn.cursor() as cur:
                                    record = transform_policy(item)
                                    load_policy(cur, record, sync_job_id)

                            success_count += 1
                            successful_policy_nos.add(record.policy["policy_no"])

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
                    full_scan_completed = True
                    break

                wait_before_next_page()
                page_num += 1

            is_complete, completion_error = evaluate_full_sync(
                full_scan_completed=full_scan_completed,
                api_total_count=api_total_count,
                totals_consistent=totals_consistent,
                processed_count=processed_count,
                success_count=success_count,
                failed_count=failed_count,
                unique_success_count=len(successful_policy_nos),
            )

            deactivated_count = 0
            with conn.transaction():
                if is_complete:
                    with conn.cursor() as cur:
                        deactivated_count = deactivate_unseen_policies(
                            cur,
                            sync_job_id,
                        )

                finish_sync_job(
                    conn,
                    sync_job_id,
                    status="SUCCEEDED" if is_complete else "PARTIAL",
                    api_total_count=api_total_count,
                    processed_count=processed_count,
                    success_count=success_count,
                    failed_count=failed_count,
                    deactivated_count=deactivated_count,
                    full_scan_completed=full_scan_completed,
                    deactivation_applied=is_complete,
                    error_message=completion_error,
                )

            if is_complete:
                print(
                    f"sync_job={sync_job_id} completed; "
                    f"deactivated={deactivated_count}"
                )
            else:
                print(
                    f"sync_job={sync_job_id} partial; "
                    f"deactivation skipped: {completion_error}"
                )

        except Exception as error:
            conn.rollback()
            with conn.transaction():
                finish_sync_job(
                    conn,
                    sync_job_id,
                    status="FAILED",
                    api_total_count=api_total_count,
                    processed_count=processed_count,
                    success_count=success_count,
                    failed_count=failed_count,
                    deactivated_count=0,
                    full_scan_completed=False,
                    deactivation_applied=False,
                    error_message=f"{type(error).__name__}: {error}",
                )
            raise

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


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
        return validate_page_size(parsed)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "page size must be a positive integer"
        ) from error


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    api_parser = sub.add_parser("api", help="Load all pages from the Youth API")
    api_parser.add_argument(
        "--page-size",
        type=positive_int,
        default=DEFAULT_PAGE_SIZE,
    )

    file_parser = sub.add_parser("file", help="Load a local JSON response")
    file_parser.add_argument("path")

    sub.add_parser(
        "backfill-periods",
        help="Re-parse stored application periods without calling the API",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

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
    elif args.command == "backfill-periods":
        processed, parsed, invalid_or_empty = backfill_application_periods()
        print(f"processed        : {processed}")
        print(f"parsed           : {parsed}")
        print(f"invalid_or_empty : {invalid_or_empty}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
