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
LOCK_NAME = "youthlink:policy-pipeline"
REPO_ROOT = Path(__file__).resolve().parents[1]
STEP_NAMES = (
    "POLICY_SYNC",
    "POLICY_CHUNKS",
    "POLICY_EMBEDDINGS",
    "RAG_PUBLISH",
    "VERIFY",
)


def build_commands(workers: int) -> dict[str, list[str] | None]:
    return {
        "POLICY_SYNC": [sys.executable, "etl/load_policies.py", "api"],
        "POLICY_CHUNKS": [
            sys.executable,
            "etl/generate_policy_chunks.py",
            "--write",
        ],
        "POLICY_EMBEDDINGS": [
            sys.executable,
            "etl/embed_policy_chunks.py",
            "--write",
            "--retry-errors",
            "--workers",
            str(workers),
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
        return "database invariant verification"
    executable = Path(command[0]).name
    return " ".join([executable, *command[1:]])


def acquire_lock(conn: psycopg.Connection[Any]) -> None:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
        (LOCK_NAME,),
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError("another policy pipeline job is already running")


def release_lock(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
        (LOCK_NAME,),
    )


def verify_pipeline_state(conn: psycopg.Connection[Any]) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE policy_row.is_active = TRUE
                  AND (
                      chunk.embedding IS NULL
                      OR chunk.embedding_error IS NOT NULL
                      OR chunk.embedding_encoding_format IS DISTINCT FROM 'float'
                  )
            ) AS policy_embedding_issues,
            COUNT(*) FILTER (WHERE policy_row.is_active = TRUE) AS active_chunks
        FROM policy_chunk AS chunk
        JOIN policy AS policy_row USING (policy_no)
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to verify policy chunks")

    web_row = conn.execute(
        """
        SELECT COUNT(*) FILTER (
            WHERE verified.embedding IS NULL
               OR source.embedding_error IS NOT NULL
               OR verified.embedding_encoding_format IS DISTINCT FROM 'float'
        ), COUNT(*)
        FROM v_verified_policy_web_chunk AS verified
        JOIN policy_web_chunk AS source USING (web_chunk_id)
        """
    ).fetchone()
    projection_row = conn.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (
                WHERE embedding_encoding_format IS DISTINCT FROM 'float'
            )
        FROM rag_retrieval_chunk
        """
    ).fetchone()
    unresolved = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM v_policy_region_resolved
            WHERE resolution_type = 'UNRESOLVED'
            """
        ).fetchone()[0]
    )
    if web_row is None or projection_row is None:
        raise RuntimeError("failed to verify web or projection chunks")

    stats = {
        "active_policy_chunks": int(row[1]),
        "policy_embedding_issues": int(row[0]),
        "verified_web_chunks": int(web_row[1]),
        "web_embedding_issues": int(web_row[0]),
        "projection_chunks": int(projection_row[0]),
        "projection_format_issues": int(projection_row[1]),
        "unresolved_regions": unresolved,
    }
    expected_projection = stats["active_policy_chunks"] + stats["verified_web_chunks"]
    stats["expected_projection_chunks"] = expected_projection
    problems = {
        key: value
        for key, value in stats.items()
        if key
        in {
            "policy_embedding_issues",
            "web_embedding_issues",
            "projection_format_issues",
            "unresolved_regions",
        }
        and value
    }
    if stats["projection_chunks"] != expected_projection:
        problems["projection_count_mismatch"] = (
            stats["projection_chunks"] - expected_projection
        )
    if problems:
        raise RuntimeError(f"pipeline verification failed: {problems}")
    return stats


def load_resume_step(conn: psycopg.Connection[Any], job_id: str) -> str:
    row = conn.execute(
        """
        SELECT status, current_step
        FROM policy_pipeline_job
        WHERE pipeline_job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"pipeline job does not exist: {job_id}")
    if row[0] != "FAILED":
        raise ValueError("only FAILED pipeline jobs can be resumed")
    return str(row[1])


def run_pipeline(
    conn: psycopg.Connection[Any],
    *,
    start_at: str,
    workers: int,
    resumed_from_job_id: str | None = None,
) -> str:
    commands = build_commands(workers)
    start_index = STEP_NAMES.index(start_at)
    job_id = str(
        conn.execute(
            """
            INSERT INTO policy_pipeline_job (
                status, current_step, resumed_from_job_id
            ) VALUES ('RUNNING', %s, %s)
            RETURNING pipeline_job_id
            """,
            (start_at, resumed_from_job_id),
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
            "UPDATE policy_pipeline_job SET current_step = %s WHERE pipeline_job_id = %s",
            (step_name, job_id),
        )
        step_id = conn.execute(
            """
            INSERT INTO policy_pipeline_step (
                pipeline_job_id, step_name, step_order, command_label
            ) VALUES (%s, %s, %s, %s)
            RETURNING pipeline_step_id
            """,
            (job_id, step_name, step_order, label),
        ).fetchone()[0]
        conn.commit()
        print(f"[{step_order}/{len(STEP_NAMES)}] {step_name}: {label}", flush=True)
        started = time.monotonic()
        exit_code = 1
        try:
            if command is None:
                step_summary = verify_pipeline_state(conn)
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
                UPDATE policy_pipeline_step
                SET status = 'SUCCEEDED', finished_at = NOW(), exit_code = 0,
                    output_tail = %s
                WHERE pipeline_step_id = %s
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
                UPDATE policy_pipeline_step
                SET status = 'FAILED', finished_at = NOW(), exit_code = %s,
                    output_tail = %s
                WHERE pipeline_step_id = %s
                """,
                (exit_code or 1, message, step_id),
            )
            conn.execute(
                """
                UPDATE policy_pipeline_job
                SET status = 'FAILED', finished_at = NOW(), error_message = %s,
                    summary = %s
                WHERE pipeline_job_id = %s
                """,
                (message, Jsonb(summary), job_id),
            )
            conn.commit()
            raise RuntimeError(f"pipeline job {job_id} failed at {step_name}") from error

    conn.execute(
        """
        UPDATE policy_pipeline_job
        SET status = 'SUCCEEDED', current_step = 'COMPLETED',
            finished_at = NOW(), summary = %s
        WHERE pipeline_job_id = %s
        """,
        (Jsonb(summary), job_id),
    )
    conn.commit()
    return job_id


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run policy sync, changed chunk generation, float embedding, "
            "RAG publish, and invariant verification under one advisory lock."
        )
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--start-at", choices=STEP_NAMES, default="POLICY_SYNC")
    parser.add_argument("--resume-job-id")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    if args.workers <= 0:
        parser.error("--workers must be greater than zero")
    if args.resume_job_id and args.start_at != "POLICY_SYNC":
        parser.error("--resume-job-id cannot be combined with --start-at")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan:
        commands = build_commands(args.workers)
        for name in STEP_NAMES[STEP_NAMES.index(args.start_at) :]:
            print(f"{name}: {command_label(commands[name])}")
        return 0

    with psycopg.connect(DB_DSN) as conn:
        acquire_lock(conn)
        try:
            start_at = args.start_at
            if args.resume_job_id:
                start_at = load_resume_step(conn, args.resume_job_id)
            job_id = run_pipeline(
                conn,
                start_at=start_at,
                workers=args.workers,
                resumed_from_job_id=args.resume_job_id,
            )
        finally:
            release_lock(conn)
    print(f"policy pipeline complete: job_id={job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
