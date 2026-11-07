from __future__ import annotations

import argparse
import json
import os
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)


def collect_health(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT status, started_at, finished_at, failed_count, error_message
            FROM policy_sync_job
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        latest_sync = cur.fetchone()
        cur.execute(
            """
            SELECT status, started_at, finished_at, current_step, error_message
            FROM policy_pipeline_job
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        latest_pipeline = cur.fetchone()
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM v_policy_region_resolved
                 WHERE resolution_type = 'UNRESOLVED') AS unresolved_regions,
                (SELECT COUNT(*) FROM policy_chunk AS chunk
                 JOIN policy AS policy_row USING (policy_no)
                 WHERE policy_row.is_active = TRUE
                   AND chunk.embedding_error IS NOT NULL) AS policy_embedding_errors,
                (SELECT COUNT(*) FROM v_verified_policy_web_chunk AS verified
                 JOIN policy_web_chunk AS chunk USING (web_chunk_id)
                 WHERE chunk.embedding_error IS NOT NULL) AS web_embedding_errors,
                (SELECT COUNT(*) FROM policy_chunk AS chunk
                 JOIN policy AS policy_row USING (policy_no)
                 WHERE policy_row.is_active = TRUE
                   AND (
                       chunk.embedding IS NULL
                       OR chunk.embedding_encoding_format IS DISTINCT FROM 'float'
                   )) AS policy_embedding_pending,
                (SELECT COUNT(*) FROM v_verified_policy_web_chunk
                 WHERE embedding IS NULL
                    OR embedding_encoding_format IS DISTINCT FROM 'float')
                    AS web_embedding_pending,
                (SELECT COUNT(*) FROM policy_web_ai_review
                 WHERE execution_status IN ('FAILED', 'INVALID_OUTPUT'))
                    AS ai_review_failures
            """
        )
        counts = dict(cur.fetchone())
    return {
        "latest_policy_sync": dict(latest_sync) if latest_sync else None,
        "latest_pipeline": dict(latest_pipeline) if latest_pipeline else None,
        **{key: int(value) for key, value in counts.items()},
    }


def health_problems(health: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    latest_sync = health.get("latest_policy_sync")
    if latest_sync is None:
        problems.append("policy sync history is missing")
    elif latest_sync["status"] != "SUCCEEDED":
        problems.append(f"latest policy sync status={latest_sync['status']}")
    latest_pipeline = health.get("latest_pipeline")
    if latest_pipeline is not None and latest_pipeline["status"] == "FAILED":
        problems.append(
            f"latest policy pipeline failed at {latest_pipeline['current_step']}"
        )
    for key in (
        "unresolved_regions",
        "policy_embedding_errors",
        "web_embedding_errors",
        "policy_embedding_pending",
        "web_embedding_pending",
    ):
        if int(health.get(key, 0)):
            problems.append(f"{key}={health[key]}")
    return problems


def json_default(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check YouthLink ETL health; exits non-zero on actionable failures."
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with psycopg.connect(DB_DSN) as conn:
        health = collect_health(conn)
    problems = health_problems(health)
    result = {"healthy": not problems, "problems": problems, **health}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=json_default, indent=2))
    else:
        print("pipeline health: " + ("HEALTHY" if not problems else "UNHEALTHY"))
        for problem in problems:
            print(f"- {problem}")
        print(
            "counts: "
            f"unresolved={health['unresolved_regions']}, "
            f"policy_pending={health['policy_embedding_pending']}, "
            f"policy_errors={health['policy_embedding_errors']}, "
            f"web_pending={health['web_embedding_pending']}, "
            f"web_errors={health['web_embedding_errors']}, "
            f"ai_review_failures={health['ai_review_failures']}"
        )
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
