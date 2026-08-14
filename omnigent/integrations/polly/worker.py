from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias, TypeVar

import httpx

from omnigent.integrations.polly.omnigent import ReviewerFailure
from omnigent.integrations.polly.review import merge_reviews
from omnigent.integrations.polly.store import Job, PollyStore

_T = TypeVar("_T")
JsonObject: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # client JSON boundary
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable_transport_error(exc: Exception) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in _RETRYABLE_HTTP_STATUSES
    )


class PollyWorker:
    def __init__(  # type: ignore[explicit-any]  # structural external clients
        self,
        store: PollyStore,
        github: Any,
        omnigent: Any,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport_attempts: int = 3,
        max_job_attempts: int = 3,
        retry_delays: tuple[float, ...] = (5, 30),
    ) -> None:
        self.store = store
        self.github = github
        self.omnigent = omnigent
        self.sleep = sleep
        self.transport_attempts = transport_attempts
        self.max_job_attempts = max_job_attempts
        self.retry_delays = retry_delays
        if max_job_attempts < 1 or len(retry_delays) < max_job_attempts - 1:
            raise ValueError("retry_delays must cover every retryable job attempt")

    async def run_once(self) -> bool:
        job = self.store.claim_next()
        if job is None:
            return False
        try:
            await self._run(job)
        except (ReviewerFailure, httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = isinstance(exc, ReviewerFailure) or _is_retryable_transport_error(exc)
            if not retryable or job.attempts >= self.max_job_attempts:
                self.store.set_state(job.id, "failed", error=str(exc))
            else:
                self.store.schedule_retry(
                    job.id,
                    delay=self.retry_delays[job.attempts - 1],
                    error=str(exc),
                )
        except Exception as exc:  # noqa: BLE001
            self.store.set_state(job.id, "failed", error=str(exc))
        return True

    async def _run(self, job: Job) -> None:
        marker = self._review_marker(job)
        existing = await self._transport(
            lambda: self.github.find_review(job.owner, job.repo, job.pr_number, marker)
        )
        pull = await self._transport(
            lambda: self.github.get_pull(job.owner, job.repo, job.pr_number)
        )
        head_sha = self._head(pull)
        if head_sha != job.head_sha:
            self.store.supersede_and_enqueue_current_head(job, head_sha)
            return
        if existing:
            await self._update_status(job, "Review published.")
            self.store.set_state(job.id, "published")
            return
        diff = await self._transport(
            lambda: self.github.get_diff(job.owner, job.repo, job.pr_number)
        )

        if job.outputs is None:
            outputs = await self.omnigent.review_pair(diff, job.review_id)
            self.store.save_outputs(job.id, outputs)
        else:
            outputs = job.outputs

        pull = await self._transport(
            lambda: self.github.get_pull(job.owner, job.repo, job.pr_number)
        )
        head_sha = self._head(pull)
        if head_sha != job.head_sha:
            self.store.supersede_and_enqueue_current_head(job, head_sha)
            return

        merged = merge_reviews(outputs, diff)
        body = self._review_body(marker, merged)
        await self._transport(
            lambda: self.github.publish_review_once(
                job.owner,
                job.repo,
                job.pr_number,
                job.head_sha,
                body,
                merged["inline_comments"],
            )
        )
        await self._update_status(job, merged["status_update"] or "Review published.")
        self.store.set_state(job.id, "published")

    async def _update_status(self, job: Job, message: str) -> None:
        body = f"{self._status_marker(job)}\n{message}"
        comment_id = await self._transport(
            lambda: self.github.upsert_status_comment(
                job.owner,
                job.repo,
                job.pr_number,
                body,
                job.status_comment_id,
            )
        )
        self.store.set_status_comment(job.id, comment_id)

    async def _transport(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        for attempt in range(self.transport_attempts):
            try:
                return await operation()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if (
                    not _is_retryable_transport_error(exc)
                    or attempt + 1 == self.transport_attempts
                ):
                    raise
                await self.sleep(2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _head(pull: JsonObject) -> str:
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(sha, str) or not sha:
            raise ValueError("GitHub pull response is missing a valid head SHA")
        return sha

    @staticmethod
    def _review_marker(job: Job) -> str:
        return f"<!-- polly-review:{job.owner}/{job.repo}#{job.pr_number}:{job.head_sha} -->"

    @staticmethod
    def _status_marker(job: Job) -> str:
        return f"<!-- polly-status:{job.owner}/{job.repo}#{job.pr_number} -->"

    @staticmethod
    def _review_body(marker: str, merged: JsonObject) -> str:
        blockers = [
            f"- `{item['path']}:{item['line']}` **{item['title']}** — {item['body']}"
            for item in merged["blocking_findings"]
        ]
        if merged["has_blocking_issues"] and not blockers:
            blockers = ["- Reviewers reported blocking issues; see summaries above."]

        def section(title: str, values: list[str]) -> str:
            return f"## {title}\n" + ("\n".join(f"- {value}" for value in values) or "- None.")

        return "\n\n".join(
            [
                f"{marker}\n**Disposition:** "
                + ("BLOCKING" if merged["has_blocking_issues"] else "NON-BLOCKING"),
                merged["summary"],
                f"**Approach:** {merged['approach_verdict']}",
                "## Blockers\n" + ("\n".join(blockers) or "- None."),
                section("Resolved previous findings", merged["resolved_previous_findings"]),
                section("Still-open previous findings", merged["still_open_previous_findings"]),
            ]
        )
