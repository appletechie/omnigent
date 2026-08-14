from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

JsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # persisted JSON

STATES = {"pending", "running", "retry_wait", "published", "failed", "superseded"}


@dataclass(frozen=True)
class Job:
    id: int
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    installation_id: int
    review_id: str
    state: str
    attempts: int
    next_attempt_at: float | None
    status_comment_id: int | None
    outputs: JsonObject | None
    error: str | None


class PollyStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha TEXT NOT NULL,
                    installation_id INTEGER NOT NULL,
                    review_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'pending','running','retry_wait','published','failed','superseded'
                        )
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    status_comment_id INTEGER,
                    outputs_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner, repo, pr_number, head_sha)
                );
                CREATE INDEX IF NOT EXISTS jobs_claimable
                    ON jobs(state, created_at, id);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "next_attempt_at" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at REAL")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def accept(
        self,
        delivery_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        installation_id: int,
    ) -> str:
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            delivery = db.execute(
                "INSERT OR IGNORE INTO deliveries(id, received_at) VALUES (?, ?)",
                (delivery_id, now),
            )
            if delivery.rowcount == 0:
                db.commit()
                return "duplicate"
            job = db.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    owner, repo, pr_number, head_sha, installation_id, review_id,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    owner,
                    repo,
                    pr_number,
                    head_sha,
                    installation_id,
                    secrets.token_hex(8),
                    now,
                    now,
                ),
            )
            revived = db.execute(
                """
                UPDATE jobs SET state = 'pending', next_attempt_at = NULL, updated_at = ?
                WHERE owner = ? AND repo = ? AND pr_number = ? AND head_sha = ?
                  AND state = 'superseded'
                """,
                (now, owner, repo, pr_number, head_sha),
            )
            if job.rowcount or revived.rowcount:
                db.execute(
                    """
                    UPDATE jobs SET state = 'superseded', updated_at = ?
                    WHERE owner = ? AND repo = ? AND pr_number = ? AND head_sha != ?
                      AND state IN ('pending', 'retry_wait')
                    """,
                    (now, owner, repo, pr_number, head_sha),
                )
            db.commit()
            return "accepted" if job.rowcount else "duplicate"

    def supersede_and_enqueue_current_head(self, job: Job, head_sha: str | None) -> None:
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if head_sha is not None:
                db.execute(
                    """
                    INSERT OR IGNORE INTO jobs(
                        owner, repo, pr_number, head_sha, installation_id, review_id,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        job.owner,
                        job.repo,
                        job.pr_number,
                        head_sha,
                        job.installation_id,
                        secrets.token_hex(8),
                        now,
                        now,
                    ),
                )
                db.execute(
                    """
                    UPDATE jobs SET state = 'pending', next_attempt_at = NULL, updated_at = ?
                    WHERE owner = ? AND repo = ? AND pr_number = ? AND head_sha = ?
                      AND state = 'superseded'
                    """,
                    (now, job.owner, job.repo, job.pr_number, head_sha),
                )
            db.execute(
                """
                UPDATE jobs SET state = 'superseded', next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (now, job.id),
            )
            db.commit()

    def recover_running(self) -> int:
        with self._connect() as db:
            result = db.execute(
                """
                UPDATE jobs SET state = 'pending', next_attempt_at = NULL, updated_at = ?
                WHERE state = 'running'
                """,
                (self.clock(),),
            )
            return result.rowcount

    def claim_next(self) -> Job | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'pending'
                   OR (state = 'retry_wait' AND next_attempt_at <= ?)
                ORDER BY created_at, id LIMIT 1
                """,
                (self.clock(),),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """
                UPDATE jobs SET state = 'running', attempts = attempts + 1,
                    next_attempt_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (self.clock(), row["id"]),
            )
            claimed = db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if claimed is None:
                raise AssertionError("claimed job disappeared")
            db.commit()
        return self._job(claimed)

    def set_state(self, job_id: int, state: str, *, error: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown job state: {state}")
        with self._connect() as db:
            db.execute(
                """
                UPDATE jobs SET state = ?, error = ?, next_attempt_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (state, error, self.clock(), job_id),
            )

    def schedule_retry(self, job_id: int, *, delay: float, error: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE jobs SET state = 'retry_wait', error = ?,
                    next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (error, self.clock() + delay, self.clock(), job_id),
            )

    def save_outputs(self, job_id: int, outputs: JsonObject) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET outputs_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(outputs, sort_keys=True), self.clock(), job_id),
            )

    def set_status_comment(self, job_id: int, comment_id: int | None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status_comment_id = ?, updated_at = ? WHERE id = ?",
                (comment_id, self.clock(), job_id),
            )

    def reset_failed(self, job_id: int) -> bool:
        with self._connect() as db:
            result = db.execute(
                """
                UPDATE jobs SET state = 'pending', attempts = 0, error = NULL,
                    next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND state = 'failed'
                """,
                (self.clock(), job_id),
            )
            return result.rowcount == 1

    def get_job(self, job_id: int) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def list_jobs(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [self._job(row) for row in rows]

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            owner=row["owner"],
            repo=row["repo"],
            pr_number=row["pr_number"],
            head_sha=row["head_sha"],
            installation_id=row["installation_id"],
            review_id=row["review_id"],
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            status_comment_id=row["status_comment_id"],
            outputs=json.loads(row["outputs_json"]) if row["outputs_json"] else None,
            error=row["error"],
        )
