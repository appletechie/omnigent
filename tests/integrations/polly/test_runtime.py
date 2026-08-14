from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from omnigent.integrations.polly.store import PollyStore
from omnigent.integrations.polly.webhook import create_webhook_app


def _payload(
    *,
    sha: str = "head-1",
    draft: bool = False,
    fork: bool = False,
    repository: str = "acme/widgets",
) -> bytes:
    owner, name = repository.split("/", 1)
    repo = {"full_name": repository, "owner": {"login": owner}, "name": name}
    head_repo = {"full_name": f"other/{name}" if fork else repository}
    return json.dumps(
        {
            "action": "opened",
            "installation": {"id": 7},
            "repository": repo,
            "pull_request": {
                "number": 12,
                "draft": draft,
                "head": {"sha": sha, "repo": head_repo},
                "base": {"repo": repo},
            },
        }
    ).encode()


def _post(client: TestClient, body: bytes, *, delivery: str = "delivery-1") -> httpx.Response:
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )


def test_webhook_verifies_filters_and_durably_deduplicates(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    client = TestClient(create_webhook_app(store, "secret", max_body_bytes=1024))

    assert client.get("/health").json() == {"status": "ok"}
    accepted = _post(client, _payload())
    duplicate = _post(client, _payload())

    assert accepted.status_code == 202
    assert accepted.json() == {"status": "accepted"}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"status": "duplicate"}
    jobs = store.list_jobs()
    assert [(job.owner, job.repo, job.pr_number, job.head_sha, job.state) for job in jobs] == [
        ("acme", "widgets", 12, "head-1", "pending")
    ]


def test_webhook_enforces_repository_allowlist_case_insensitively(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    client = TestClient(create_webhook_app(store, "secret", repositories=("ACME/WIDGETS",)))

    allowed = _post(client, _payload(), delivery="allowed")
    denied = _post(client, _payload(repository="acme/other"), delivery="denied")

    assert allowed.json() == {"status": "accepted"}
    assert denied.json() == {"status": "skipped"}
    assert [(job.owner, job.repo) for job in store.list_jobs()] == [("acme", "widgets")]


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "bad"}, _payload(), 401),
        ({}, b"x" * 1025, 413),
    ],
)
def test_webhook_rejects_bad_signature_and_oversized_raw_body(
    tmp_path: Path, headers: dict[str, str], body: bytes, status: int
) -> None:
    store = PollyStore(tmp_path / "polly.db")
    client = TestClient(create_webhook_app(store, "secret", max_body_bytes=1024))

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == status
    assert store.list_jobs() == []


@pytest.mark.parametrize("body", [b"[]", b'{"action":"opened","pull_request":"bad"}'])
def test_webhook_rejects_malformed_signed_payload(tmp_path: Path, body: bytes) -> None:
    store = PollyStore(tmp_path / "polly.db")
    client = TestClient(create_webhook_app(store, "secret"), raise_server_exceptions=False)

    response = _post(client, body)

    assert response.status_code == 400
    assert store.list_jobs() == []


@pytest.mark.parametrize(
    ("event", "action", "draft", "fork"),
    [
        ("push", "opened", False, False),
        ("pull_request", "closed", False, False),
        ("pull_request", "opened", True, False),
        ("pull_request", "opened", False, True),
    ],
)
def test_webhook_skips_unsupported_or_unsafe_pull_requests(
    tmp_path: Path, event: str, action: str, draft: bool, fork: bool
) -> None:
    store = PollyStore(tmp_path / "polly.db")
    client = TestClient(create_webhook_app(store, "secret"))
    body = json.loads(_payload(draft=draft, fork=fork))
    body["action"] = action
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhooks/github",
        content=raw,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": f"{event}-{action}-{draft}-{fork}",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 202
    assert response.json() == {"status": "skipped"}
    assert store.list_jobs() == []


def test_store_recovers_supersedes_and_only_resets_failed_jobs(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("d1", "acme", "widgets", 12, "old", 7)
    old = store.claim_next()
    assert old is not None and old.state == "running"

    store.recover_running()
    assert store.get_job(old.id).state == "pending"
    store.accept("d2", "acme", "widgets", 12, "new", 7)
    assert store.get_job(old.id).state == "superseded"

    new = next(job for job in store.list_jobs() if job.head_sha == "new")
    assert store.reset_failed(new.id) is False
    store.set_state(new.id, "failed", error="model failed")
    assert store.reset_failed(new.id) is True
    assert store.get_job(new.id).state == "pending"


def test_store_accept_revives_an_existing_superseded_head(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("old", "acme", "widgets", 12, "old-head", 7)
    store.accept("new", "acme", "widgets", 12, "new-head", 7)

    assert store.accept("back-push", "acme", "widgets", 12, "old-head", 7) == "duplicate"

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("old-head", "pending"),
        ("new-head", "superseded"),
    ]


@pytest.mark.parametrize("terminal_state", ["published", "failed"])
def test_store_accept_does_not_revive_terminal_head(tmp_path: Path, terminal_state: str) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("first", "acme", "widgets", 12, "head", 7)
    job = store.list_jobs()[0]
    store.set_state(job.id, terminal_state)

    assert store.accept("again", "acme", "widgets", 12, "head", 7) == "duplicate"
    assert store.get_job(job.id).state == terminal_state


@pytest.mark.parametrize("terminal_state", ["published", "failed"])
def test_terminal_stale_delivery_does_not_supersede_current_pending_head(
    tmp_path: Path, terminal_state: str
) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("old", "acme", "widgets", 12, "old-head", 7)
    old = store.list_jobs()[0]
    store.set_state(old.id, terminal_state)
    store.accept("current", "acme", "widgets", 12, "current-head", 7)

    assert store.accept("delayed-old", "acme", "widgets", 12, "old-head", 7) == "duplicate"
    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("old-head", terminal_state),
        ("current-head", "pending"),
    ]


def test_claim_next_returns_claimed_job_without_second_store_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)

    def fail_second_read(_: int) -> None:
        raise AssertionError("claim_next must return from its transaction")

    monkeypatch.setattr(store, "get_job", fail_second_read)
    claimed = store.claim_next()

    assert claimed is not None
    assert (claimed.state, claimed.attempts) == ("running", 1)


def test_reopening_store_does_not_steal_live_job_and_explicit_recovery_resets_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "polly.db"
    store = PollyStore(path)
    store.accept("d1", "acme", "widgets", 12, "head", 7)
    running = store.claim_next()
    assert running is not None and running.state == "running"

    reopened = PollyStore(path)

    assert reopened.get_job(running.id).state == "running"
    assert reopened.recover_running() == 1
    assert reopened.get_job(running.id).state == "pending"


def test_retry_wait_jobs_are_claimed_only_when_due_and_stop_at_attempt_ceiling(
    tmp_path: Path,
) -> None:
    now = [100.0]
    store = PollyStore(tmp_path / "polly.db", clock=lambda: now[0])
    store.accept("d1", "acme", "widgets", 12, "head", 7)
    first = store.claim_next()
    assert first is not None
    store.schedule_retry(first.id, delay=5, error="network")

    assert store.claim_next() is None
    now[0] = 105.0
    second = store.claim_next()

    assert second is not None
    assert second.attempts == 2
    assert second.next_attempt_at is None
