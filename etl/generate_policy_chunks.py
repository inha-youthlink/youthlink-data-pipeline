from __future__ import annotations

import argparse
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()


DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)

CHUNKER_VERSION = "policy-v1"
CHUNK_INDEX = {
    "SUMMARY": 0,
    "ELIGIBILITY": 1,
    "SUPPORT": 2,
    "APPLICATION": 3,
}

ELIGIBILITY_CODE_GROUPS = {
    "MAJOR": "plcyMajorCd",
    "JOB": "jobCd",
    "SCHOOL": "schoolCd",
    "SPECIAL": "sbizCd",
}

ELIGIBILITY_LABELS = {
    "MAJOR": "전공 조건",
    "JOB": "취업 상태",
    "SCHOOL": "학력 조건",
    "SPECIAL": "특화 대상",
}


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    chunk_type: str
    content: str
    content_hash: str


@dataclass(frozen=True)
class RegionCatalog:
    all_codes: frozenset[str]
    codes_by_sido: Mapping[str, frozenset[str]]
    sido_names: Mapping[str, str]


def normalize_text(value: Any) -> str:
    """Normalize API text without deleting meaningful angle-bracket labels."""
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\N", "\n")
    text = text.replace("\ufeff", "").replace("\u200b", "")

    lines = [line.strip() for line in text.split("\n")]
    normalized: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank

    return "\n".join(normalized).strip()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _unique_texts(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _format_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return normalize_text(value)


def _format_period(start: Any, end: Any, extra: Any = None) -> str:
    start_text = _format_date(start)
    end_text = _format_date(end)
    extra_text = normalize_text(extra)

    if start_text and end_text:
        period = f"{start_text} ~ {end_text}"
    else:
        period = start_text or end_text

    if period and extra_text and extra_text != period:
        return f"{period} ({extra_text})"
    return period or extra_text


def _format_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return normalize_text(value)


def _code_name(
    code_lookup: Mapping[tuple[str, str], str],
    group: str,
    code: Any,
) -> str:
    code_text = normalize_text(code)
    if not code_text:
        return ""
    return code_lookup.get((group, code_text), code_text)


def build_region_catalog(rows: Iterable[Mapping[str, Any]]) -> RegionCatalog:
    all_codes: set[str] = set()
    mutable_by_sido: dict[str, set[str]] = defaultdict(set)
    sido_names: dict[str, str] = {}

    for row in rows:
        code = normalize_text(row.get("region_code"))
        sido_code = normalize_text(row.get("sido_code"))
        sido_name = normalize_text(row.get("sido_name"))
        if not code or not sido_code:
            continue
        all_codes.add(code)
        mutable_by_sido[sido_code].add(code)
        if sido_name:
            sido_names[sido_code] = sido_name

    return RegionCatalog(
        all_codes=frozenset(all_codes),
        codes_by_sido={
            key: frozenset(value) for key, value in mutable_by_sido.items()
        },
        sido_names=sido_names,
    )


def compress_regions(
    catalog: RegionCatalog,
    rows: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Return deterministic, human-readable labels for resolved active regions."""
    by_code: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        code = normalize_text(row.get("resolved_region_code") or row.get("region_code"))
        if code and code in catalog.all_codes:
            by_code.setdefault(code, row)

    selected_codes = set(by_code)
    if catalog.all_codes and selected_codes == set(catalog.all_codes):
        return ["전국"]

    labels: list[str] = []
    selected_by_sido: dict[str, set[str]] = defaultdict(set)
    for code, row in by_code.items():
        sido_code = normalize_text(row.get("sido_code"))
        if sido_code:
            selected_by_sido[sido_code].add(code)

    for sido_code in sorted(selected_by_sido):
        selected = selected_by_sido[sido_code]
        full_set = set(catalog.codes_by_sido.get(sido_code, frozenset()))
        if full_set and selected == full_set:
            sido_name = catalog.sido_names.get(sido_code, sido_code)
            labels.append(f"{sido_name} 전체")
            continue

        for code in sorted(selected):
            region_name = normalize_text(by_code[code].get("region_name"))
            labels.append(region_name or code)

    return labels


def _append(lines: list[str], label: str, value: Any) -> None:
    text = normalize_text(value)
    if text:
        lines.append(f"{label}: {text}")


def _render_chunk(chunk_type: str, policy_name: str, fields: Sequence[str]) -> Chunk:
    content = "\n".join([f"정책명: {policy_name}", *fields])
    content = normalize_text(content)
    return Chunk(
        chunk_index=CHUNK_INDEX[chunk_type],
        chunk_type=chunk_type,
        content=content,
        content_hash=content_hash(content),
    )


def build_policy_chunks(
    policy: Mapping[str, Any],
    region_labels: Sequence[str],
    eligibility_codes: Mapping[str, Sequence[str]],
    code_lookup: Mapping[tuple[str, str], str],
) -> list[Chunk]:
    policy_name = normalize_text(policy.get("policy_name"))
    if not policy_name:
        raise ValueError("policy_name is required to build chunks")

    summary: list[str] = []
    _append(summary, "정책 설명", policy.get("description"))
    keywords = _unique_texts(policy.get("keywords") or [])
    if keywords:
        summary.append(f"키워드: {', '.join(keywords)}")
    large_categories = _unique_texts(policy.get("large_categories") or [])
    if large_categories:
        summary.append(f"대분류: {', '.join(large_categories)}")
    middle_categories = _unique_texts(policy.get("middle_categories") or [])
    if middle_categories:
        summary.append(f"중분류: {', '.join(middle_categories)}")
    provider = _code_name(
        code_lookup, "pvsnInstGroupCd", policy.get("provider_group_code")
    )
    _append(summary, "제공기관 구분", provider)
    _append(summary, "주관기관", policy.get("supervising_org_name"))
    _append(summary, "운영기관", policy.get("operating_org_name"))

    eligibility: list[str] = []
    if region_labels:
        eligibility.append(f"적용 지역: {', '.join(region_labels)}")

    min_age = policy.get("min_age")
    max_age = policy.get("max_age")
    if min_age is not None and max_age is not None:
        eligibility.append(f"연령: 만 {min_age}세 ~ {max_age}세")
    elif min_age is not None:
        eligibility.append(f"연령: 만 {min_age}세 이상")
    elif max_age is not None:
        eligibility.append(f"연령: 만 {max_age}세 이하")
    elif policy.get("age_limit_yn") is False:
        eligibility.append("연령: 제한 없음")

    marriage = _code_name(
        code_lookup, "mrgSttsCd", policy.get("marriage_status_code")
    )
    _append(eligibility, "혼인 조건", marriage)
    income_condition = _code_name(
        code_lookup, "earnCndSeCd", policy.get("income_condition_code")
    )
    _append(eligibility, "소득 조건", income_condition)
    min_income = _format_number(policy.get("min_income"))
    max_income = _format_number(policy.get("max_income"))
    if min_income:
        eligibility.append(f"소득 하한: {min_income}")
    if max_income:
        eligibility.append(f"소득 상한: {max_income}")
    _append(eligibility, "소득 기타 조건", policy.get("income_etc"))

    for condition_type in ("MAJOR", "JOB", "SCHOOL", "SPECIAL"):
        codes = eligibility_codes.get(condition_type, ())
        names = _unique_texts(
            _code_name(code_lookup, ELIGIBILITY_CODE_GROUPS[condition_type], code)
            for code in codes
        )
        if names:
            eligibility.append(
                f"{ELIGIBILITY_LABELS[condition_type]}: {', '.join(names)}"
            )
    _append(eligibility, "추가 자격", policy.get("additional_qualification"))
    _append(eligibility, "참여 제외 대상", policy.get("participation_exclusion"))

    support: list[str] = []
    _append(support, "지원 내용", policy.get("support_content"))
    if policy.get("support_scale_limit") is False:
        support.append("지원 규모: 제한 없음")
    elif policy.get("support_scale_count") is not None:
        support.append(
            f"지원 규모: {_format_number(policy.get('support_scale_count'))}명"
        )
    if policy.get("first_come_first_served") is not None:
        support.append(
            "선착순 여부: "
            + ("예" if policy.get("first_come_first_served") else "아니오")
        )
    provision_method = _code_name(
        code_lookup, "plcyPvsnMthdCd", policy.get("provision_method_code")
    )
    _append(support, "제공 방식", provision_method)
    business_type = _code_name(
        code_lookup, "bizPrdSeCd", policy.get("business_period_type_code")
    )
    _append(support, "사업 기간 구분", business_type)
    business_period = _format_period(
        policy.get("business_start_date"),
        policy.get("business_end_date"),
        policy.get("business_period_etc"),
    )
    _append(support, "사업 기간", business_period)
    _append(support, "기타 안내", policy.get("additional_notes"))

    application: list[str] = []
    application_period_raw = policy.get("application_period_raw")
    if policy.get("application_period_parse_status") == "INVALID":
        application_period_raw = None
    application_period = _format_period(
        policy.get("application_start_date"),
        policy.get("application_end_date"),
        application_period_raw,
    )
    application_details = [
        application_period,
        normalize_text(policy.get("application_method")),
        normalize_text(policy.get("screening_method")),
        normalize_text(policy.get("submission_documents")),
    ]
    application_urls = [
        normalize_text(policy.get("application_url")),
        normalize_text(policy.get("reference_url_1")),
        normalize_text(policy.get("reference_url_2")),
    ]
    # URLs are metadata and are not embedded, but their presence means the
    # application status still has value when the policy has no other details.
    if any(application_details) or any(application_urls):
        application_type = _code_name(
            code_lookup, "aplyPrdSeCd", policy.get("application_period_type_code")
        )
        _append(application, "신청 기간 구분", application_type)
        _append(application, "신청 기간", application_period)
        _append(application, "신청 방법", policy.get("application_method"))
        _append(application, "심사 방법", policy.get("screening_method"))
        _append(application, "제출 서류", policy.get("submission_documents"))

    chunks = [_render_chunk("SUMMARY", policy_name, summary)]
    if eligibility:
        chunks.append(_render_chunk("ELIGIBILITY", policy_name, eligibility))
    if support:
        chunks.append(_render_chunk("SUPPORT", policy_name, support))
    if application:
        chunks.append(_render_chunk("APPLICATION", policy_name, application))
    return chunks


def _load_policies(
    conn: psycopg.Connection[Any],
    policy_nos: Sequence[str],
    limit: int | None,
) -> list[dict[str, Any]]:
    # Inactive policies are retained for audit/history, but must not produce
    # new RAG chunks. Existing chunks are intentionally left untouched so a
    # later reactivation can reuse them until the normal hash comparison runs.
    conditions: list[str] = ["is_active = TRUE"]
    params: list[Any] = []
    if policy_nos:
        conditions.append("policy_no = ANY(%s)")
        params.append(list(policy_nos))

    sql = "SELECT * FROM policy"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY policy_no"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _load_context(
    conn: psycopg.Connection[Any],
    policy_nos: Sequence[str],
) -> tuple[
    RegionCatalog,
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, list[str]]],
    dict[tuple[str, str], str],
]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT region_code, sido_code, sido_name, region_name
            FROM region
            WHERE is_active = TRUE
            ORDER BY sido_code, region_code
            """
        )
        catalog = build_region_catalog(cur.fetchall())

        cur.execute(
            """
            SELECT DISTINCT
                policy_no, resolved_region_code, sido_code, sido_name, region_name
            FROM v_policy_region_resolved
            WHERE policy_no = ANY(%s)
              AND resolution_type <> 'UNRESOLVED'
              AND is_active = TRUE
            ORDER BY policy_no, sido_code, resolved_region_code
            """,
            (list(policy_nos),),
        )
        region_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cur.fetchall():
            region_rows[row["policy_no"]].append(row)

        cur.execute(
            """
            SELECT policy_no, condition_type, condition_code
            FROM policy_eligibility_code
            WHERE policy_no = ANY(%s)
            ORDER BY policy_no, condition_type, condition_code
            """,
            (list(policy_nos),),
        )
        eligibility: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in cur.fetchall():
            eligibility[row["policy_no"]][row["condition_type"]].append(
                row["condition_code"]
            )

        cur.execute("SELECT code_group, code, code_name FROM common_code")
        code_lookup = {
            (row["code_group"], row["code"]): row["code_name"]
            for row in cur.fetchall()
        }

    return catalog, region_rows, eligibility, code_lookup


def generate_chunks(
    conn: psycopg.Connection[Any],
    policy_nos: Sequence[str] = (),
    limit: int | None = None,
) -> dict[str, list[Chunk]]:
    policies = _load_policies(conn, policy_nos, limit)
    selected = [row["policy_no"] for row in policies]
    if not selected:
        return {}

    catalog, region_rows, eligibility, code_lookup = _load_context(conn, selected)
    result: dict[str, list[Chunk]] = {}
    for policy in policies:
        policy_no = policy["policy_no"]
        labels = compress_regions(catalog, region_rows.get(policy_no, ()))
        result[policy_no] = build_policy_chunks(
            policy,
            labels,
            eligibility.get(policy_no, {}),
            code_lookup,
        )
    return result


def verify_chunk_schema(conn: psycopg.Connection[Any]) -> None:
    required = {
        "content_hash",
        "chunker_version",
        "token_count",
        "embedded_at",
        "embedding_error",
        "updated_at",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'policy_chunk'
            """
        )
        existing = {row[0] for row in cur.fetchall()}
    missing = sorted(required - existing)
    if missing:
        raise RuntimeError(
            "policy_chunk schema is not ready; apply "
            "db/migrations/005_prepare_policy_chunk_pipeline.sql first. "
            f"Missing columns: {', '.join(missing)}"
        )


def sync_chunks(
    conn: psycopg.Connection[Any],
    generated: Mapping[str, Sequence[Chunk]],
) -> dict[str, int]:
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    policy_nos = list(generated)
    if not policy_nos:
        return stats

    verify_chunk_schema(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT chunk_id, policy_no, chunk_index, chunk_type, content,
                   content_hash, chunker_version
            FROM policy_chunk
            WHERE policy_no = ANY(%s)
            """,
            (policy_nos,),
        )
        existing_rows = list(cur.fetchall())

        existing_by_key = {
            (row["policy_no"], row["chunk_type"]): row for row in existing_rows
        }
        desired_by_key = {
            (policy_no, chunk.chunk_type): chunk
            for policy_no, chunks in generated.items()
            for chunk in chunks
        }

        delete_ids: list[Any] = []
        for row in existing_rows:
            desired = desired_by_key.get((row["policy_no"], row["chunk_type"]))
            if desired is None or row["chunk_index"] != desired.chunk_index:
                delete_ids.append(row["chunk_id"])
        if delete_ids:
            cur.execute("DELETE FROM policy_chunk WHERE chunk_id = ANY(%s)", (delete_ids,))
            stats["deleted"] += cur.rowcount

        deleted_set = set(delete_ids)
        for policy_no, chunks in generated.items():
            for chunk in chunks:
                old = existing_by_key.get((policy_no, chunk.chunk_type))
                if old is not None and old["chunk_id"] in deleted_set:
                    old = None

                if (
                    old is not None
                    and old["content"] == chunk.content
                    and old["content_hash"] == chunk.content_hash
                    and old["chunker_version"] == CHUNKER_VERSION
                ):
                    stats["unchanged"] += 1
                    continue

                if old is None:
                    cur.execute(
                        """
                        INSERT INTO policy_chunk (
                            policy_no, chunk_index, chunk_type, content,
                            content_hash, chunker_version
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            policy_no,
                            chunk.chunk_index,
                            chunk.chunk_type,
                            chunk.content,
                            chunk.content_hash,
                            CHUNKER_VERSION,
                        ),
                    )
                    stats["inserted"] += 1
                else:
                    cur.execute(
                        """
                        UPDATE policy_chunk
                        SET chunk_index = %s,
                            content = %s,
                            content_hash = %s,
                            chunker_version = %s,
                            token_count = NULL,
                            embedding = NULL,
                            embedding_model = NULL,
                            embedded_at = NULL,
                            embedding_error = NULL,
                            updated_at = NOW()
                        WHERE chunk_id = %s
                        """,
                        (
                            chunk.chunk_index,
                            chunk.content,
                            chunk.content_hash,
                            CHUNKER_VERSION,
                            old["chunk_id"],
                        ),
                    )
                    stats["updated"] += 1

    return stats


def _print_preview(generated: Mapping[str, Sequence[Chunk]], sample_size: int) -> None:
    total = sum(len(chunks) for chunks in generated.values())
    print(f"generated {total} chunks for {len(generated)} policies (dry-run)")
    for policy_no, chunks in list(generated.items())[:sample_size]:
        print(f"\n[{policy_no}]")
        for chunk in chunks:
            print(f"\n--- {chunk.chunk_index} {chunk.chunk_type} ---")
            print(chunk.content)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate deterministic RAG chunks from YouthLink policies."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write generated chunks to PostgreSQL (default: dry-run)",
    )
    parser.add_argument(
        "--policy-no",
        action="append",
        default=[],
        help="process only this policy number; may be repeated",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="maximum number of policies to process",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=2,
        help="number of policies printed during dry-run (default: 2)",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be greater than zero")
    if args.sample_size < 0:
        parser.error("--sample-size must not be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    limit = args.limit
    if not args.write and limit is None and not args.policy_no:
        limit = 5

    with psycopg.connect(DB_DSN) as conn:
        generated = generate_chunks(conn, args.policy_no, limit)
        if args.policy_no:
            missing = sorted(set(args.policy_no) - set(generated))
            if missing:
                raise RuntimeError(f"Unknown policy_no: {', '.join(missing)}")

        if not args.write:
            _print_preview(generated, args.sample_size)
            return 0

        stats = sync_chunks(conn, generated)
        conn.commit()

    total = sum(len(chunks) for chunks in generated.values())
    print(
        f"synced {total} chunks for {len(generated)} policies: "
        f"inserted={stats['inserted']}, updated={stats['updated']}, "
        f"unchanged={stats['unchanged']}, deleted={stats['deleted']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
