from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

TERMINAL_PIPELINE_STATUSES = {
    "success",
    "failed",
    "canceled",
    "skipped",
    "manual",
}


@dataclass(frozen=True)
class E2EConfig:
    gitlab_url: str
    manytask_url: str
    gitlab_token: str
    git_username: str

    student_project: str
    course_name: str
    task_name: str
    solution_path: str
    branch: str

    expected_partial_score: int
    expected_full_score: int
    pipeline_timeout: int

    @classmethod
    def from_env(cls) -> "E2EConfig":
        required = {
            "E2E_GITLAB_TOKEN": os.getenv("E2E_GITLAB_TOKEN"),
            "E2E_STUDENT_PROJECT": os.getenv("E2E_STUDENT_PROJECT"),
            "E2E_COURSE": os.getenv("E2E_COURSE"),
            "E2E_TASK": os.getenv("E2E_TASK"),
            "E2E_SOLUTION_PATH": os.getenv("E2E_SOLUTION_PATH"),
        }

        return cls(
            gitlab_url=os.getenv(
                "E2E_GITLAB_URL",
                "http://localhost:8929",
            ).rstrip("/"),
            manytask_url=os.getenv(
                "E2E_MANYTASK_URL",
                "http://localhost:8000",
            ).rstrip("/"),
            gitlab_token=required["E2E_GITLAB_TOKEN"] or "",
            git_username=os.getenv(
                "E2E_GIT_USERNAME",
                "oauth2",
            ),
            student_project=required["E2E_STUDENT_PROJECT"] or "",
            course_name=required["E2E_COURSE"] or "",
            task_name=required["E2E_TASK"] or "",
            solution_path=required["E2E_SOLUTION_PATH"] or "",
            branch=os.getenv("E2E_BRANCH", "main"),
            expected_partial_score=int(os.getenv("E2E_PARTIAL_SCORE", "67")),
            expected_full_score=int(os.getenv("E2E_FULL_SCORE", "5")),
            pipeline_timeout=int(os.getenv("E2E_PIPELINE_TIMEOUT", "1200")),
        )


class E2EEnvironment:
    def __init__(self, config: E2EConfig, workdir: Path) -> None:
        self.config = config
        self.workdir = workdir

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["PRIVATE-TOKEN"] = config.gitlab_token

    @property
    def encoded_project(self) -> str:
        return quote(
            self.config.student_project,
            safe="",
        )

    def assert_ready(self) -> None:
        self._gitlab("GET", "version")

        response = requests.get(f"{self.config.manytask_url}/healthcheck", timeout=10)
        response.raise_for_status()
        project = self.project()

        if project.get("archived"):
            raise AssertionError("Student project is archived")

    def _gitlab(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, (f"{self.config.gitlab_url}" f"/api/v4/{path.lstrip('/')}"),
                                                timeout=60, **kwargs)

        if response.status_code >= 400:
            raise AssertionError(
                f"GitLab {method} {path} failed: "
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            )

        if not response.content:
            return None

        return response.json()

    def project(self) -> dict[str, Any]:
        return self._gitlab("GET",f"projects/{self.encoded_project}")

    def clone_student_repo(self) -> Path:
        repo = self.workdir / "student"

        if repo.exists():
            return repo

        clone_url = f"{self.config.gitlab_url}/" f"{self.config.student_project}.git"

        env = self._git_env()

        self._run(["git", "clone", clone_url, str(repo)], env=env)
        self._run(["git", "checkout", self.config.branch], cwd=repo, env=env)

        return repo

    def push_solution(self, solution: str, commit_message: str) -> str:
        repo = self.clone_student_repo()

        target = repo / self.config.solution_path

        if not target.parent.exists():
            raise AssertionError(
                "Solution directory does not exist: "
                f"{target.parent}. "
                "Check E2E_SOLUTION_PATH."
            )

        target.write_text(
            solution,
            encoding="utf-8",
        )

        env = self._git_env()

        self._run(
            [
                "git",
                "add",
                self.config.solution_path,
            ],
            cwd=repo,
            env=env,
        )

        status = self._run(
            [
                "git",
                "status",
                "--porcelain",
            ],
            cwd=repo,
            env=env,
            capture=True,
        )

        if not status.stdout.strip():
            raise AssertionError(
                "Solution did not change repository. "
                "Reset student repo or change fixture."
            )

        self._run(
            [
                "git",
                "-c",
                "user.name=Checker E2E",
                "-c",
                "user.email=checker-e2e@example.test",
                "commit",
                "-m",
                commit_message,
            ],
            cwd=repo,
            env=env,
        )

        sha = self._run(
            [
                "git",
                "rev-parse",
                "HEAD",
            ],
            cwd=repo,
            env=env,
            capture=True,
        ).stdout.strip()

        self._run(
            [
                "git",
                "push",
                "origin",
                f"HEAD:{self.config.branch}",
            ],
            cwd=repo,
            env=env,
        )

        return sha

    def wait_pipeline_for_sha(
        self,
        sha: str,
    ) -> dict[str, Any]:
        project_id = self.project()["id"]

        deadline = time.monotonic() + self.config.pipeline_timeout

        pipeline = None

        while time.monotonic() < deadline:
            pipelines = self._gitlab(
                "GET",
                f"projects/{project_id}/pipelines",
                params={
                    "sha": sha,
                    "per_page": 20,
                },
            )

            if pipelines:
                pipeline = pipelines[0]
                break

            time.sleep(2)

        if pipeline is None:
            raise AssertionError(
                f"Push {sha} did not create pipeline. " "Check .gitlab-ci.yml rules."
            )

        pipeline_id = pipeline["id"]
        previous_status = None

        while time.monotonic() < deadline:
            current = self._gitlab(
                "GET",
                (f"projects/{project_id}/" f"pipelines/{pipeline_id}"),
            )

            status = current["status"]

            if status != previous_status:
                print(
                    f"[checker-e2e] " f"pipeline {pipeline_id}: {status}",
                    flush=True,
                )

                previous_status = status

            if status in TERMINAL_PIPELINE_STATUSES:
                self._save_job_logs(
                    project_id,
                    pipeline_id,
                )

                if status not in {
                    "success",
                    "manual",
                }:
                    raise AssertionError(
                        f"Pipeline {pipeline_id} "
                        f"finished with {status}. "
                        f"Logs: {self.workdir}"
                    )

                return current

            time.sleep(3)

        self._save_job_logs(
            project_id,
            pipeline_id,
        )

        raise TimeoutError(
            f"Pipeline {pipeline_id} exceeded " f"{self.config.pipeline_timeout}s"
        )

    def assert_grade_job_present(
        self,
        pipeline_id: int,
    ) -> None:
        project_id = self.project()["id"]

        jobs = self._gitlab(
            "GET",
            (f"projects/{project_id}/" f"pipelines/{pipeline_id}/jobs"),
            params={"per_page": 100},
        )

        names = {job["name"] for job in jobs}

        if "grade" not in names:
            raise AssertionError(
                f"Pipeline {pipeline_id} "
                f"has no grade job. "
                f"Jobs: {sorted(names)}"
            )

    def score(self, course_token: str) -> int:
        response = requests.get(
            (f"{self.config.manytask_url}" f"/api/{self.config.course_name}/score"),
            headers={
                "Authorization": f"Bearer {course_token}",
            },
            data={
                "username": self.student_username(),
                "task": self.config.task_name,
            },
            timeout=30,
        )

        if response.status_code >= 400:
            raise AssertionError(
                "Manytask score request failed: "
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            )

        payload = response.json()

        if payload.get("task") != self.config.task_name:
            raise AssertionError(f"Unexpected response: {payload}")

        return int(payload["score"])

    def student_username(self) -> str:
        project = self.project()

        return project["path_with_namespace"].split("/")[-1]

    def _save_job_logs(self, project_id: int, pipeline_id: int) -> None:
        jobs = self._gitlab(
            "GET",
            (f"projects/{project_id}/" f"pipelines/{pipeline_id}/jobs"),
            params={"per_page": 100},
        )

        for job in jobs:
            response = self.session.get(
                (
                    f"{self.config.gitlab_url}"
                    f"/api/v4/projects/{project_id}"
                    f"/jobs/{job['id']}/trace"
                ),
                timeout=30,
            )

            name = job["name"].replace(
                "/",
                "_",
            )

            path = self.workdir / (f"pipeline-{pipeline_id}-" f"{name}-{job['id']}.log")

            path.write_text(
                response.text,
                encoding="utf-8",
            )

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()

        env["GIT_TERMINAL_PROMPT"] = "0"
        env["E2E_GIT_TOKEN"] = self.config.gitlab_token
        env["E2E_GIT_USERNAME"] = self.config.git_username

        askpass = self.workdir / "askpass.sh"

        askpass.write_text(
            """#!/bin/sh
case "$1" in
  *Username*)
    printf "%s\\n" "$E2E_GIT_USERNAME"
    ;;
  *)
    printf "%s\\n" "$E2E_GIT_TOKEN"
    ;;
esac
""",
            encoding="utf-8",
        )

        askpass.chmod(0o700)

        env["GIT_ASKPASS"] = str(askpass)

        return env

    @staticmethod
    def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                capture: bool = False) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        if result.returncode != 0:
            raise AssertionError(
                "Command failed "
                f"({result.returncode}): "
                f"{' '.join(args)}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )

        if not capture:
            if result.stdout:
                print(
                    result.stdout,
                    end="",
                )

            if result.stderr:
                print(
                    result.stderr,
                    end="",
                )

        return result
