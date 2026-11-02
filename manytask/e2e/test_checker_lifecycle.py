from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

import pytest


def make_unique(solution: str) -> str:
    return f"{solution.rstrip()}\n\n# E2E run: {uuid4().hex}\n"


def expect_checker_failure(env, commit_sha: str) -> int:
    try:
        pipeline = env.wait_pipeline_for_sha(commit_sha)

    except AssertionError as exc:
        error_message = str(exc)

        pipeline_match = re.search(
            r"Pipeline (\d+) finished with failed",
            error_message,
        )

        logs_match = re.search(
            r"Logs: (.+)",
            error_message,
        )

        if not pipeline_match or not logs_match:
            raise

        pipeline_id = int(pipeline_match.group(1))
        log_dir = Path(logs_match.group(1).strip())

        grade_logs = list(log_dir.glob(f"pipeline-{pipeline_id}-grade-*.log"))

        if not grade_logs:
            raise AssertionError("Grade job log was not found") from exc

        log_text = "\n".join(
            path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            for path in grade_logs
        )

        failed_tasks = re.search(
            r"Task pipelines failed:\s*(\[[^\r\n]*\])",
            log_text,
        )

        if (
            "TESTING FAILED" not in log_text
            or failed_tasks is None
            or env.config.task_name not in failed_tasks.group(1)
        ):
            raise AssertionError(
                "Pipeline failed for an unexpected reason. " f"Logs: {grade_logs[0]}"
            ) from exc

        env.assert_grade_job_present(pipeline_id)

        print(
            f"\n[E2E] Incorrect solution rejected "
            f"as expected. Pipeline: {pipeline_id}",
            flush=True,
        )

        return pipeline_id

    else:
        raise AssertionError(
            "Incorrect solution unexpectedly passed! " f"Pipeline: {pipeline['id']}"
        )


def test_checker_grade_lifecycle(
    env,
    partial_solution,
    correct_solution,
):
    course_token = os.getenv("E2E_MANYTASK_TOKEN")

    if not course_token:
        pytest.fail(
            "Set E2E_MANYTASK_TOKEN to the course token "
            "used by the Manytask score API."
        )
    print(
        "\n[E2E] STEP 1: Sending incorrect solution...",
        flush=True,
    )

    partial_sha = env.push_solution(
        make_unique(partial_solution),
        "E2E incorrect solution for sum_a_b",
    )

    partial_pipeline_id = expect_checker_failure(
        env,
        partial_sha,
    )

    print(
        f"[E2E] Incorrect solution processed. " f"Pipeline: {partial_pipeline_id}",
        flush=True,
    )

    print(
        "\n[E2E] STEP 2: Sending correct solution...",
        flush=True,
    )

    full_sha = env.push_solution(
        make_unique(correct_solution),
        "E2E correct solution for sum_a_b",
    )

    full_pipeline = env.wait_pipeline_for_sha(full_sha)

    env.assert_grade_job_present(full_pipeline["id"])

    print(
        f"\n[E2E] Correct solution pipeline succeeded: " f"{full_pipeline['id']}",
        flush=True,
    )

    actual_score = env.score(course_token)

    print(
        f"[E2E] Actual score: {actual_score}",
        flush=True,
    )

    expected_score = env.config.expected_full_score

    assert actual_score == expected_score, (
        f"Expected score {expected_score}, " f"got {actual_score}"
    )

    print(
        "\n[E2E] Incorrect -> Correct lifecycle completed!",
        flush=True,
    )
