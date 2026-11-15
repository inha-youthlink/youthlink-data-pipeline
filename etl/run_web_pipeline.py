from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


load_dotenv()

DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/youthlink",
)
DEFAULT_CRAWL_LIMIT = int(os.getenv("WEB_PIPELINE_CRAWL_LIMIT", "50"))
DEFAULT_PER_HOST_LIMIT = int(os.getenv("WEB_PIPELINE_PER_HOST_LIMIT", "2"))
DEFAULT_AI_REVIEW_LIMIT = int(os.getenv("WEB_PIPELINE_AI_REVIEW_LIMIT", "20"))
DEFAULT_REVALIDATE_LIMIT = int(os.getenv("WEB_PIPELINE_REVALIDATE_LIMIT", "500"))
DEFAULT_POLICY_MAX_AGE_HOURS = float(
    os.getenv("WEB_PIPELINE_POLICY_MAX_AGE_HOURS", "6")
)
DEFAULT_POLICY_WAIT_MINUTES = float(
    os.getenv("WEB_PIPELINE_POLICY_WAIT_MINUTES", "30")
)
DEFAULT_RESUME_MAX_AGE_HOURS = float(
    os.getenv("WEB_PIPELINE_RESUME_MAX_AGE_HOURS", "12")
)
LOCK_NAME = "youthlink:web-pipeline"
REPO_ROOT = Path(__file__).resolve().parents[1]
STEP_NAMES = (
    "SOURCE_SYNC",
    "CRAWL",
    "REVALIDATE",
    "AI_SHADOW",
    "AI_APPLY",
    "WEB_CHUNKS",
    "WEB_EMBEDDINGS",
    "RAG_PUBLISH",
    "VERIFY",
)


def build_commands(
    *,
    crawl_limit: int,
    per_host_limit: int,
    ai_review_limit: int,
    revalidate_limit: int,
) -> dict[str, list[str] | None]:
    return {
        "SOURCE_SYNC": [
            sys.executable,
            "etl/sync_policy_sources.py",
            "--write",
            "--top-domains",
            "0",
            "--show-issues",
            "0",
        ],
        "CRAWL": [
            sys.executable,
            "etl/crawl_policy_sources.py",
            "--write",
            "--limit",
            str(crawl_limit),
            "--per-host-limit",
            str(per_host_limit),
        ],
        "REVALIDATE": [
            sys.executable,
            "etl/revalidate_policy_sources.py",
            "--write",
            "--limit",
            str(revalidate_limit),
        ],
        "AI_SHADOW": [
            sys.executable,
            "etl/ai_review_policy_sources.py",
            "--shadow",
            "--skip-cached",
            "--write",
            "--limit",
            str(ai_review_limit),
        ],
        "AI_APPLY": [
            sys.executable,
            "etl/ai_review_policy_sources.py",
            "--apply-safe-observations",
            "--write",
            "--limit",
            str(ai_review_limit),
        ],
        "WEB_CHUNKS": [
            sys.executable,
            "etl/generate_policy_web_chunks.py",
            "--write",
        ],
        "WEB_EMBEDDINGS": [
            sys.executable,
            "etl/embed_policy_web_chunks.py",
            "--write",
            "--retry-errors",
        ],
        "RAG_PUBLISH": [
            sys.executable,
            "etl/sync_rag_retrieval_chunks.py",
            "--write",
        ],
        "VERIFY": None,
    }


def command_label(command: Sequence[str] | None) -> str:
    if command is None:
        return "web pipeline database invariant verification"
    executable = Path(command[0]).name
    return " ".join([executable, *command[1:]])


def acquire_lock(conn: psycopg.Connection[Any]) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
        (LOCK_NAME,),
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError("another web pipeline job is already running")


def release_lock(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
        (LOCK_NAME,),
    )


def ensure_policy_pipeline_ready(
    conn: psycopg.Connection[Any],
    max_age_hours: float,
) -> None:
    row = conn.execute(
        """
        SELECT status, current_step,
               finished_at >= NOW() - (%s * INTERVAL '1 hour') AS is_recent
        FROM policy_pipeline_job
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (max_age_hours,),
    ).fetchone()
    if row is None:
        raise RuntimeError("policy pipeline history is missing")
    if row[0] != "SUCCEEDED":
        raise RuntimeError(
            f"latest policy pipeline is {row[0]} at {row[1]}; "
            "web pipeline will retry after policy data is healthy"
        )
    if not row[2]:
        raise RuntimeError(
            "latest successful policy pipeline is too old; "
            "web pipeline will retry after the scheduled policy refresh"
        )


def wait_for_policy_pipeline_ready(
    conn: psycopg.Connection[Any],
    *,
    max_age_hours: float,
    wait_minutes: float,
    poll_seconds: float = 15.0,
) -> None:
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        try:
            ensure_policy_pipeline_ready(conn, max_age_hours)
            return
        except RuntimeError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"policy pipeline did not become ready within "
                    f"{wait_minutes:g} minutes: {error}"
                ) from error
            print(
                "waiting for policy pipeline: "
                f"{error}; remaining_seconds={round(remaining)}",
                flush=True,
            )
            conn.commit()
            time.sleep(min(poll_seconds, remaining))


def verify_web_pipeline_state(conn: psycopg.Connection[Any]) -> dict[str, int]:
    policy_row = conn.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (
            WHERE chunk.embedding IS NULL
               OR chunk.embedding_error IS NOT NULL
               OR chunk.embedding_encoding_format IS DISTINCT FROM 'float'
        )
        FROM policy_chunk AS chunk
        JOIN policy AS policy_row USING (policy_no)
        WHERE policy_row.is_active = TRUE
        """
    ).fetchone()
    web_row = conn.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (
            WHERE verified.embedding IS NULL
               OR source.embedding_error IS NOT NULL
               OR verified.embedding_encoding_format IS DISTINCT FROM 'float'
        )
        FROM v_verified_policy_web_chunk AS verified
        JOIN policy_web_chunk AS source USING (web_chunk_id)
        """
    ).fetchone()
    projection_row = conn.execute(
        """
        SELECT COUNT(*), COUNT(*) FILTER (
            WHERE embedding_encoding_format IS DISTINCT FROM 'float'
        )
        FROM rag_retrieval_chunk
        """
    ).fetchone()
    if policy_row is None or web_row is None or projection_row is None:
        raise RuntimeError("failed to verify web retrieval state")

    stats = {
        "active_policy_chunks": int(policy_row[0]),
        "policy_embedding_issues": int(policy_row[1]),
        "verified_web_chunks": int(web_row[0]),
        "web_embedding_issues": int(web_row[1]),
        "projection_chunks": int(projection_row[0]),
        "projection_format_issues": int(projection_row[1]),
    }
    stats["expected_projection_chunks"] = (
        stats["active_policy_chunks"] + stats["verified_web_chunks"]
    )
    problems = {
        key: value
        for key, value in stats.items()
        if key.endswith("_issues") and value
    }
    if stats["projection_chunks"] != stats["expected_projection_chunks"]:
        problems["projection_count_mismatch"] = (
            stats["projection_chunks"] - stats["expected_projection_chunks"]
        )
    if problems:
        raise RuntimeError(f"web pipeline verification failed: {problems}")
    return stats


def fail_orphaned_jobs(conn: psycopg.Connection[Any]) -> list[str]:
    message = "web pipeline process ended before recording completion"
    conn.execute(
        """
        UPDATE web_pipeline_step AS step
        SET status = 'FAILED', finished_at = NOW(), exit_code = -1,
            output_tail = %s
        FROM web_pipeline_job AS job
        WHERE step.web_pipeline_job_id = job.web_pipeline_job_id
          AND job.status = 'RUNNING'
          AND step.status = 'RUNNING'
        """,
        (message,),
    )
    rows = conn.execute(
        """
        UPDATE web_pipeline_job
        SET status = 'FAILED', finished_at = NOW(), error_message = %s
        WHERE status = 'RUNNING'
        RETURNING web_pipeline_job_id
        """,
        (message,),
    ).fetchall()
    conn.commit()
    return [str(row[0]) for row in rows]


def load_resume_step(conn: psycopg.Connection[Any], job_id: str) -> str:
    row = conn.execute(
        """
        SELECT status, current_step
        FROM web_pipeline_job
        WHERE web_pipeline_job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"web pipeline job does not exist: {job_id}")
    if row[0] != "FAILED":
        raise ValueError("only FAILED web pipeline jobs can be resumed")
    return str(row[1])


def load_latest_failed_job(
    conn: psycopg.Connection[Any],
    max_age_hours: float,
) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT web_pipeline_job_id, status, current_step,
               started_at >= NOW() - (%s * INTERVAL '1 hour') AS is_recent
        FROM web_pipeline_job
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (max_age_hours,),
    ).fetchone()
    if row is None or row[1] != "FAILED" or not row[3]:
        return None
    return str(row[0]), str(row[2])


def run_pipeline(
    conn: psycopg.Connection[Any],
    *,
    start_at: str,
    crawl_limit: int,
    per_host_limit: int,
    ai_review_limit: int,
    revalidate_limit: int,
    resumed_from_job_id: str | None = None,
) -> str:
    commands = build_commands(
        crawl_limit=crawl_limit,
        per_host_limit=per_host_limit,
        ai_review_limit=ai_review_limit,
        revalidate_limit=revalidate_limit,
    )
    start_index = STEP_NAMES.index(start_at)
    job_id = str(
        conn.execute(
            """
            INSERT INTO web_pipeline_job (
                status, current_step, resumed_from_job_id,
                crawl_limit, per_host_limit, ai_review_limit
            ) VALUES ('RUNNING', %s, %s, %s, %s, %s)
            RETURNING web_pipeline_job_id
            """,
            (
                start_at,
                resumed_from_job_id,
                crawl_limit,
                per_host_limit,
                ai_review_limit,
            ),
        ).fetchone()[0]
    )
    conn.commit()
    summary: dict[str, Any] = {}

    for step_order, step_name in enumerate(STEP_NAMES, start=1):
        if step_order - 1 < start_index:
            continue
        command = commands[step_name]
        label = command_label(command)
        conn.execute(
            "UPDATE web_pipeline_job SET current_step = %s "
            "WHERE web_pipeline_job_id = %s",
            (step_name, job_id),
        )
        step_id = conn.execute(
            """
            INSERT INTO web_pipeline_step (
                web_pipeline_job_id, step_name, step_order, command_label
            ) VALUES (%s, %s, %s, %s)
            RETURNING web_pipeline_step_id
            """,
            (job_id, step_name, step_order, label),
        ).fetchone()[0]
        conn.commit()
        print(f"[{step_order}/{len(STEP_NAMES)}] {step_name}: {label}", flush=True)
        started = time.monotonic()
        exit_code = 1
        try:
            if command is None:
                step_summary = verify_web_pipeline_state(conn)
                output = json.dumps(step_summary, ensure_ascii=False, sort_keys=True)
                exit_code = 0
            else:
                completed = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                output = (completed.stdout + completed.stderr).strip()
                exit_code = completed.returncode
                step_summary = {
                    "exit_code": exit_code,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                if exit_code != 0:
                    raise RuntimeError(
                        f"{step_name} exited with {exit_code}: {output[-2000:]}"
                    )
            summary[step_name] = step_summary
            conn.execute(
                """
                UPDATE web_pipeline_step
                SET status = 'SUCCEEDED', finished_at = NOW(), exit_code = 0,
                    output_tail = %s
                WHERE web_pipeline_step_id = %s
                """,
                (output[-8000:], step_id),
            )
            conn.commit()
            if output:
                print(output[-2000:], flush=True)
        except Exception as error:
            conn.rollback()
            message = f"{type(error).__name__}: {error}"[:8000]
            conn.execute(
                """
                UPDATE web_pipeline_step
                SET status = 'FAILED', finished_at = NOW(), exit_code = %s,
                    output_tail = %s
                WHERE web_pipeline_step_id = %s
                """,
                (exit_code or 1, message, step_id),
            )
            conn.execute(
                """
                UPDATE web_pipeline_job
                SET status = 'FAILED', finished_at = NOW(), error_message = %s,
                    summary = %s
                WHERE web_pipeline_job_id = %s
                """,
                (message, Jsonb(summary), job_id),
            )
            conn.commit()
            raise RuntimeError(
                f"web pipeline job {job_id} failed at {step_name}"
            ) from error

    conn.execute(
        """
        UPDATE web_pipeline_job
        SET status = 'SUCCEEDED', current_step = 'COMPLETED',
            finished_at = NOW(), summary = %s
        WHERE web_pipeline_job_id = %s
        """,
        (Jsonb(summary), job_id),
    )
    conn.commit()
    return job_id


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded web discovery, crawling, AI evidence review, web "
            "embedding, RAG publish, and verification under one lock."
        )
    )
    parser.add_argument("--crawl-limit", type=int, default=DEFAULT_CRAWL_LIMIT)
    parser.add_argument(
        "--per-host-limit",
        type=int,
        default=DEFAULT_PER_HOST_LIMIT,
    )
    parser.add_argument(
        "--ai-review-limit",
        type=int,
        default=DEFAULT_AI_REVIEW_LIMIT,
    )
    parser.add_argument(
        "--revalidate-limit",
        type=int,
        default=DEFAULT_REVALIDATE_LIMIT,
    )
    parser.add_argument(
        "--policy-max-age-hours",
        type=float,
        default=DEFAULT_POLICY_MAX_AGE_HOURS,
    )
    parser.add_argument(
        "--policy-wait-minutes",
        type=float,
        default=DEFAULT_POLICY_WAIT_MINUTES,
    )
    parser.add_argument("--start-at", choices=STEP_NAMES, default="SOURCE_SYNC")
    parser.add_argument("--resume-job-id")
    parser.add_argument("--resume-latest-failed", action="store_true")
    parser.add_argument(
        "--resume-max-age-hours",
        type=float,
        default=DEFAULT_RESUME_MAX_AGE_HOURS,
    )
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    for name in (
        "crawl_limit",
        "per_host_limit",
        "ai_review_limit",
        "revalidate_limit",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    if args.resume_max_age_hours <= 0:
        parser.error("--resume-max-age-hours must be greater than zero")
    if args.policy_max_age_hours <= 0:
        parser.error("--policy-max-age-hours must be greater than zero")
    if args.policy_wait_minutes < 0:
        parser.error("--policy-wait-minutes must not be negative")
    if args.resume_job_id and args.resume_latest_failed:
        parser.error(
            "--resume-job-id cannot be combined with --resume-latest-failed"
        )
    if (args.resume_job_id or args.resume_latest_failed) and (
        args.start_at != "SOURCE_SYNC"
    ):
        parser.error("resume options cannot be combined with --start-at")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan:
        commands = build_commands(
            crawl_limit=args.crawl_limit,
            per_host_limit=args.per_host_limit,
            ai_review_limit=args.ai_review_limit,
            revalidate_limit=args.revalidate_limit,
        )
        for name in STEP_NAMES[STEP_NAMES.index(args.start_at) :]:
            print(f"{name}: {command_label(commands[name])}")
        return 0

    with psycopg.connect(DB_DSN) as conn:
        acquire_lock(conn)
        try:
            wait_for_policy_pipeline_ready(
                conn,
                max_age_hours=args.policy_max_age_hours,
                wait_minutes=args.policy_wait_minutes,
            )
            orphaned_job_ids = fail_orphaned_jobs(conn)
            if orphaned_job_ids:
                print(
                    "closed orphaned web pipeline jobs: "
                    + ", ".join(orphaned_job_ids),
                    flush=True,
                )
            start_at = args.start_at
            resumed_from_job_id: str | None = None
            if args.resume_job_id:
                start_at = load_resume_step(conn, args.resume_job_id)
                resumed_from_job_id = args.resume_job_id
            elif args.resume_latest_failed:
                latest_failed = load_latest_failed_job(
                    conn,
                    args.resume_max_age_hours,
                )
                if latest_failed is not None:
                    resumed_from_job_id, start_at = latest_failed
                    print(
                        f"resuming recent failed web job {resumed_from_job_id} "
                        f"from {start_at}",
                        flush=True,
                    )
            job_id = run_pipeline(
                conn,
                start_at=start_at,
                crawl_limit=args.crawl_limit,
                per_host_limit=args.per_host_limit,
                ai_review_limit=args.ai_review_limit,
                revalidate_limit=args.revalidate_limit,
                resumed_from_job_id=resumed_from_job_id,
            )
        finally:
            release_lock(conn)
    print(f"web pipeline complete: job_id={job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
