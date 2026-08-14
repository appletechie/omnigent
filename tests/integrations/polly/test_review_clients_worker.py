from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from omnigent.integrations.polly.github import GitHubAppClient
from omnigent.integrations.polly.omnigent import OmnigentClient, ReviewerFailure
from omnigent.integrations.polly.review import (
    ReviewValidationError,
    extract_review,
    merge_reviews,
    review_schema,
)
from omnigent.integrations.polly.store import PollyStore
from omnigent.integrations.polly.worker import PollyWorker


def _review(review_id: str, *, verdict: str = "Sound", priority: str = "Low") -> dict[str, Any]:
    return {
        "review_id": review_id,
        "summary": "Found one issue.",
        "approach_verdict": verdict,
        "has_blocking_issues": priority == "High",
        "status_update": "Review complete.",
        "resolved_previous_findings": ["fixed", "shared"],
        "still_open_previous_findings": ["open"],
        "inline_comments": [
            {
                "path": "app.py",
                "line": 2,
                "priority": priority,
                "title": "Unsafe branch",
                "body": "This branch can lose data.",
            }
        ],
    }


def test_strict_review_id_json_rejects_mismatch_and_extra_fields() -> None:
    with pytest.raises(ReviewValidationError, match="Review-ID"):
        extract_review(json.dumps(_review("wrong")), "expected")
    extra = _review("expected") | {"unexpected": True}
    with pytest.raises(ReviewValidationError, match="unexpected"):
        extract_review(json.dumps(extra), "expected")
    nested = _review("expected") | {"workspace_status": {"note": "ok", "secret": "leak"}}
    with pytest.raises(ReviewValidationError, match="workspace_status"):
        extract_review(json.dumps(nested), "expected")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", "   \t"),
        ("title", "\n\t"),
        ("body", "   "),
        ("path", "   "),
        ("path", "/absolute.py"),
        ("path", "./local.py"),
        ("path", "../outside.py"),
        ("path", "a/header.py"),
        ("path", "b/header.py"),
    ],
)
def test_prompt_schema_rejects_every_string_runtime_validation_rejects(
    field: str, value: str
) -> None:
    schema = review_schema("rid")
    if field == "summary":
        field_schema = schema["properties"][field]
    else:
        field_schema = schema["properties"]["inline_comments"]["items"]["properties"][field]
    pattern = field_schema.get("pattern")
    schema_accepts = len(value) >= field_schema.get("minLength", 0) and (
        pattern is None or re.search(pattern, value) is not None
    )

    review = _review("rid")
    if field == "summary":
        review[field] = value
    else:
        review["inline_comments"][0][field] = value
    try:
        extract_review(json.dumps(review), "rid")
    except ReviewValidationError:
        runtime_accepts = False
    else:
        runtime_accepts = True

    assert schema_accepts is False
    assert runtime_accepts is False


def test_merge_is_deterministic_and_demotes_invalid_anchors() -> None:
    claude = _review("rid", verdict="Mostly sound", priority="Low")
    codex = _review("rid", verdict="Needs changes", priority="High")
    codex["summary"] = "Found a blocker."
    codex["resolved_previous_findings"] = ["shared", "other"]
    codex["still_open_previous_findings"] = ["other-open"]
    codex["inline_comments"].append(
        {
            "path": "missing.py",
            "line": 99,
            "priority": "Medium",
            "title": "Unanchored",
            "body": "Keep this finding in the body.",
        }
    )
    claude["inline_comments"].append(
        {
            "path": "missing.py",
            "line": 99,
            "priority": "Low",
            "title": "Duplicate unanchored",
            "body": "This lower-priority duplicate must be dropped.",
        }
    )
    diff = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,2 +1,2 @@",
            " one",
            "+two",
            "-old",
        ]
    )

    merged = merge_reviews({"claude_review": claude, "codex_review": codex}, diff)

    assert merged["approach_verdict"] == "Needs changes"
    assert merged["has_blocking_issues"] is True
    assert merged["resolved_previous_findings"] == ["shared"]
    assert merged["still_open_previous_findings"] == ["open", "other-open"]
    assert merged["inline_comments"] == [codex["inline_comments"][0]]
    assert "## Claude" in merged["summary"] and "## Codex" in merged["summary"]
    assert "missing.py:99" in merged["summary"]
    assert merged["summary"].count("missing.py:99") == 1
    assert "**Medium · Unanchored**" in merged["summary"]


@pytest.mark.asyncio
async def test_github_app_auth_caches_token_and_publishes_review_once() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(
                201, json={"token": "install-token", "expires_at": "2030-01-01T00:00:00Z"}
            )
        if path == "/repos/acme/widgets/pulls/12/reviews" and request.method == "GET":
            return httpx.Response(200, json=[])
        if path == "/repos/acme/widgets/pulls/12/reviews":
            return httpx.Response(200, json={"id": 99})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    review_body = PollyWorker._review_body(
        "<!-- polly-review:key -->",
        {
            "summary": "## Claude\nFound one issue.\n\n## Codex\nConfirmed it.",
            "approach_verdict": "Needs changes",
            "has_blocking_issues": True,
            "blocking_findings": [
                {
                    "path": "app.py",
                    "line": 2,
                    "title": "Unsafe branch",
                    "body": "This branch can lose data.",
                }
            ],
            "resolved_previous_findings": ["fixed"],
            "still_open_previous_findings": ["open"],
        },
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.github.test") as http:
        github = GitHubAppClient(
            "123", key, http, app_slug="polly-review", clock=lambda: 1_700_000_000.0
        )
        first = await github.publish_review_once("acme", "widgets", 12, "head", review_body, [])
        second = await github.installation_token("acme", "widgets")

    assert first == {"id": 99}
    assert second == "install-token"
    assert (
        Counter(request.url.path for request in requests)["/app/installations/7/access_tokens"]
        == 1
    )
    app_claims = jwt.decode(
        requests[0].headers["Authorization"].removeprefix("Bearer "),
        options={"verify_signature": False},
    )
    assert app_claims == {"iat": 1_699_999_940, "exp": 1_700_000_480, "iss": "123"}
    publish = requests[-1]
    assert publish.headers["Authorization"] == "Bearer install-token"
    assert json.loads(publish.content) == {
        "event": "COMMENT",
        "commit_id": "head",
        "body": """<!-- polly-review:key -->
**Disposition:** BLOCKING

## Claude
Found one issue.

## Codex
Confirmed it.

**Approach:** Needs changes

## Blockers
- `app.py:2` **Unsafe branch** — This branch can lose data.

## Resolved previous findings
- fixed

## Still-open previous findings
- open""",
        "comments": [],
    }


@pytest.mark.asyncio
async def test_github_updates_status_comment_found_after_first_page() -> None:
    writes: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if request.url.path == "/app/installations/7/access_tokens":
            return httpx.Response(
                201,
                json={"token": "install-token", "expires_at": "2030-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/acme/widgets/issues/12/comments":
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[{"id": i, "body": "other"} for i in range(100)])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 123,
                        "body": "<!-- polly-status:key -->\nold",
                        "user": {"login": "polly-review[bot]"},
                    }
                ],
            )
        if request.url.path == "/repos/acme/widgets/issues/comments/123":
            writes.append(request)
            return httpx.Response(200, json={"id": 123})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    ) as http:
        github = GitHubAppClient(
            "123", key, http, app_slug="polly-review", clock=lambda: 1_700_000_000.0
        )
        comment_id = await github.upsert_status_comment(
            "acme", "widgets", 12, "<!-- polly-status:key -->\nnew"
        )

    assert comment_id == 123
    assert len(writes) == 1
    assert json.loads(writes[0].content) == {"body": "<!-- polly-status:key -->\nnew"}


@pytest.mark.asyncio
async def test_github_marker_lookup_requires_exact_bot_and_paginates_to_short_page() -> None:
    marker = "<!-- polly-review:key -->"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if request.url.path == "/app/installations/7/access_tokens":
            return httpx.Response(
                201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
            )
        if request.url.path == "/repos/acme/widgets/pulls/12/reviews":
            page = int(request.url.params["page"])
            if page <= 10:
                batch = [{"id": page * 100 + i, "body": "other"} for i in range(100)]
                if page == 1:
                    batch[0] = {
                        "id": 1,
                        "body": f"prefix {marker}",
                        "user": {"login": "polly-review[bot]"},
                    }
                    batch[1] = {
                        "id": 2,
                        "body": f"{marker}\nforged",
                        "user": {"login": "attacker"},
                    }
                return httpx.Response(200, json=batch)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9999,
                        "body": f"{marker}\nreal",
                        "user": {"login": "polly-review[bot]"},
                    }
                ],
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    ) as http:
        github = GitHubAppClient(
            "123", key, http, app_slug="polly-review", clock=lambda: 1_700_000_000.0
        )
        found = await github.find_review("acme", "widgets", 12, marker)

    assert found is not None and found["id"] == 9999


@pytest.mark.asyncio
async def test_github_invalidates_installation_token_and_retries_once_on_401() -> None:
    minted = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal minted
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if request.url.path == "/app/installations/7/access_tokens":
            minted += 1
            return httpx.Response(
                201,
                json={"token": f"token-{minted}", "expires_at": "2030-01-01T00:00:00Z"},
            )
        if request.url.path == "/repos/acme/widgets/pulls/12":
            if request.headers["Authorization"] == "Bearer token-1":
                return httpx.Response(401)
            return httpx.Response(200, json={"head": {"sha": "head"}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    ) as http:
        github = GitHubAppClient(
            "123", key, http, app_slug="polly-review", clock=lambda: 1_700_000_000.0
        )
        pull = await github.get_pull("acme", "widgets", 12)

    assert pull["head"] == {"sha": "head"}
    assert minted == 2


@pytest.mark.asyncio
async def test_github_caps_diff_at_utf8_boundary_with_truncation_notice() -> None:
    raw = ("a" * 399_950 + "€" * 100).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if request.url.path == "/app/installations/7/access_tokens":
            return httpx.Response(
                201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
            )
        return httpx.Response(200, content=raw)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    ) as http:
        github = GitHubAppClient(
            "123", key, http, app_slug="polly-review", clock=lambda: 1_700_000_000.0
        )
        diff = await github.get_diff("acme", "widgets", 12)

    assert len(diff.encode()) <= 400_000
    assert "Diff truncated" in diff
    diff.encode("utf-8")


@pytest.mark.asyncio
async def test_omnigent_refreshes_and_runs_exact_two_reviewers_with_one_upload() -> None:
    calls: list[tuple[str, str, Any]] = []
    parent_polls = 0
    snapshots = {
        "claude": _review("rid", verdict="Mostly sound"),
        "codex": _review("rid", verdict="Sound"),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal parent_polls
        body: Any = None
        if request.headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(request.content)
        calls.append((request.method, request.url.path, body))
        if (
            request.url.path == "/v1/sessions"
            and request.headers.get("Authorization") == "Bearer expired"
        ):
            return httpx.Response(401)
        if request.url.path == "/oauth/token":
            assert request.headers["X-Omnigent-Client-Secret"] == "client-secret"
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rotated"})
        if request.url.path == "/v1/sessions" and body.get("parent_session_id") is None:
            return httpx.Response(200, json={"id": "parent"})
        if request.url.path == "/v1/sessions":
            name = body["sub_agent_name"]
            return httpx.Response(
                200, json={"id": "claude" if name == "claude_review" else "codex"}
            )
        if request.url.path.endswith("/resources/files"):
            return httpx.Response(200, json={"id": "parent-file"})
        if request.url.path.endswith("/resources/files:copy"):
            child = request.url.path.split("/")[3]
            return httpx.Response(
                200, json={"mapping": {"parent-file": {"new_id": f"{child}-file"}}}
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(204)
        if request.url.path == "/v1/sessions/parent":
            parent_polls += 1
            return httpx.Response(
                200,
                json={
                    "status": "idle",
                    "runner_online": parent_polls > 1,
                    "runner_id": "runner" if parent_polls > 1 else None,
                    "items": [],
                },
            )
        if request.url.path == "/v1/sessions/claude":
            return httpx.Response(
                200, json={"status": "idle", "items": [_assistant(snapshots["claude"])]}
            )
        if request.url.path == "/v1/sessions/codex":
            return httpx.Response(
                200, json={"status": "idle", "items": [_assistant(snapshots["codex"])]}
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as http:
        client = OmnigentClient(
            http,
            access_token="expired",
            refresh_token="refresh",
            device_client_secret="client-secret",
            agent_id="polly-review",
            host_id="host",
            workspace="/tmp/polly",
            sleep=lambda _: asyncio.sleep(0),
        )
        results = await client.review_pair("diff", "rid")

    assert set(results) == {"claude_review", "codex_review"}
    assert sum(path.endswith("/resources/files") for _, path, _ in calls) == 1
    children = [
        body
        for method, path, body in calls
        if method == "POST" and path == "/v1/sessions" and body and body.get("parent_session_id")
    ]
    assert [child["sub_agent_name"] for child in children] == ["claude_review", "codex_review"]
    turns = [body for method, path, body in calls if method == "POST" and path.endswith("/events")]
    assert all(turn["data"]["content"][1]["type"] == "input_file" for turn in turns)
    prompts = [turn["data"]["content"][0]["text"] for turn in turns]
    assert all('"additionalProperties":false' in prompt for prompt in prompts)
    assert all(
        '"enum":["Sound","Mostly sound","Unclear","Risky","Needs changes"]' in prompt
        for prompt in prompts
    )
    assert all('"const":"rid"' in prompt for prompt in prompts)
    first_child = next(
        i
        for i, call in enumerate(calls)
        if call[:2] == ("POST", "/v1/sessions") and call[2].get("parent_session_id")
    )
    parent_gets = [i for i, call in enumerate(calls) if call[:2] == ("GET", "/v1/sessions/parent")]
    assert len(parent_gets) == 2 and parent_gets[-1] < first_child
    assert client.refresh_token == "rotated"


@pytest.mark.asyncio
async def test_omnigent_initial_idle_snapshot_waits_for_runner_cycle() -> None:
    snapshots = iter(
        [
            {"status": "idle", "items": []},
            {"status": "running", "items": []},
            {"status": "idle", "items": [_assistant(_review("rid"))]},
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/child"
        return httpx.Response(200, json=next(snapshots))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as http:
        client = OmnigentClient(
            http,
            access_token="token",
            refresh_token="refresh",
            agent_id="polly-review",
            host_id="host",
            workspace="/tmp/polly",
            sleep=lambda _: asyncio.sleep(0),
        )
        result = await client._poll("child", "rid")

    assert result["review_id"] == "rid"


@pytest.mark.asyncio
async def test_omnigent_interrupts_started_child_when_later_setup_fails() -> None:
    interrupts: list[str] = []
    child_creates = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal child_creates
        path = request.url.path
        body = (
            json.loads(request.content)
            if request.headers.get("Content-Type", "").startswith("application/json")
            else {}
        )
        if path == "/v1/sessions" and not body.get("parent_session_id"):
            return httpx.Response(200, json={"id": "parent"})
        if path == "/v1/sessions/parent/resources/files":
            return httpx.Response(200, json={"id": "file"})
        if path == "/v1/sessions/parent":
            return httpx.Response(
                200, json={"runner_online": True, "runner_id": "runner", "status": "idle"}
            )
        if path == "/v1/sessions":
            child_creates += 1
            return (
                httpx.Response(200, json={"id": "claude"})
                if child_creates == 1
                else httpx.Response(500)
            )
        if path == "/v1/sessions/claude/resources/files:copy":
            return httpx.Response(200, json={"mapping": {"file": {"new_id": "child-file"}}})
        if path in {"/v1/sessions/parent/events", "/v1/sessions/claude/events"}:
            if body.get("type") == "interrupt":
                interrupts.append(path.split("/")[3])
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as http:
        client = OmnigentClient(
            http,
            access_token="token",
            refresh_token="refresh",
            agent_id="polly-review",
            host_id="host",
            workspace="/tmp/polly",
            sleep=lambda _: asyncio.sleep(0),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.review_pair("diff", "rid")

    assert sorted(interrupts) == ["claude", "parent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True], ids=["copy-http-failure", "copy-map"])
async def test_omnigent_interrupts_parent_and_child_when_copy_setup_fails(
    malformed: bool,
) -> None:
    interrupts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = (
            json.loads(request.content)
            if request.headers.get("Content-Type", "").startswith("application/json")
            else {}
        )
        if path == "/v1/sessions" and not body.get("parent_session_id"):
            return httpx.Response(200, json={"id": "parent"})
        if path == "/v1/sessions/parent/resources/files":
            return httpx.Response(200, json={"id": "file"})
        if path == "/v1/sessions/parent":
            return httpx.Response(
                200, json={"runner_online": True, "runner_id": "runner", "status": "idle"}
            )
        if path == "/v1/sessions":
            return httpx.Response(200, json={"id": "claude"})
        if path == "/v1/sessions/claude/resources/files:copy":
            return httpx.Response(200, json={"mapping": {}}) if malformed else httpx.Response(500)
        if path in {"/v1/sessions/parent/events", "/v1/sessions/claude/events"}:
            if body.get("type") == "interrupt":
                interrupts.append(path.split("/")[3])
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as http:
        client = OmnigentClient(
            http,
            access_token="token",
            refresh_token="refresh",
            agent_id="polly-review",
            host_id="host",
            workspace="/tmp/polly",
            sleep=lambda _: asyncio.sleep(0),
        )
        with pytest.raises((httpx.HTTPStatusError, ReviewerFailure)):
            await client.review_pair("diff", "rid")

    assert sorted(interrupts) == ["claude", "parent"]


@pytest.mark.asyncio
async def test_omnigent_interrupts_both_started_children_when_polling_fails() -> None:
    interrupts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = (
            json.loads(request.content)
            if request.headers.get("Content-Type", "").startswith("application/json")
            else {}
        )
        if path == "/v1/sessions" and not body.get("parent_session_id"):
            return httpx.Response(200, json={"id": "parent"})
        if path == "/v1/sessions/parent/resources/files":
            return httpx.Response(200, json={"id": "file"})
        if path == "/v1/sessions/parent":
            return httpx.Response(
                200, json={"runner_online": True, "runner_id": "runner", "status": "idle"}
            )
        if path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"id": "claude" if body["sub_agent_name"] == "claude_review" else "codex"},
            )
        if path.endswith("/resources/files:copy"):
            child = path.split("/")[3]
            return httpx.Response(200, json={"mapping": {"file": {"new_id": f"{child}-file"}}})
        if path.endswith("/events"):
            child = path.split("/")[3]
            if body.get("type") == "interrupt":
                interrupts.append(child)
            return httpx.Response(204)
        if path == "/v1/sessions/claude":
            return httpx.Response(200, json={"status": "failed", "items": []})
        if path == "/v1/sessions/codex":
            return httpx.Response(200, json={"status": "running", "items": []})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://omni.test"
    ) as http:
        client = OmnigentClient(
            http,
            access_token="token",
            refresh_token="refresh",
            agent_id="polly-review",
            host_id="host",
            workspace="/tmp/polly",
            sleep=lambda _: asyncio.sleep(10),
        )
        with pytest.raises(ReviewerFailure):
            await client.review_pair("diff", "rid")

    assert sorted(interrupts) == ["claude", "codex", "parent"]


def _assistant(review: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(review)
    middle = len(text) // 2
    return {
        "type": "message",
        "data": {
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": text[:middle]},
                {"type": "output_text", "text": text[middle:]},
            ],
        },
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.head = "head"
        self.reviews: list[dict[str, Any]] = []
        self.statuses: list[str] = []

    async def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return {"head": {"sha": self.head}}

    async def get_diff(self, owner: str, repo: str, number: int) -> str:
        return "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+new"

    async def find_review(
        self, owner: str, repo: str, number: int, marker: str
    ) -> dict[str, Any] | None:
        return next((review for review in self.reviews if marker in review["body"]), None)

    async def publish_review_once(
        self,
        owner: str,
        repo: str,
        number: int,
        head_sha: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = await self.find_review(owner, repo, number, body.split("\n", 1)[0])
        if existing:
            return existing
        review = {"id": len(self.reviews) + 1, "body": body, "comments": comments}
        self.reviews.append(review)
        return review

    async def upsert_status_comment(
        self, owner: str, repo: str, number: int, body: str, cached_id: int | None = None
    ) -> int:
        self.statuses[:] = [body]
        return 10


class FakeOmnigent:
    def __init__(self) -> None:
        self.calls = 0

    async def review_pair(self, diff: str, review_id: str) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return {
            "claude_review": _review(review_id, verdict="Mostly sound"),
            "codex_review": _review(review_id, verdict="Needs changes", priority="High"),
        }


@pytest.mark.asyncio
async def test_worker_end_to_end_two_outputs_create_one_review(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = FakeGitHub()
    omnigent = FakeOmnigent()
    worker = PollyWorker(store, github, omnigent)

    assert await worker.run_once() is True
    assert len(github.reviews) == 1
    assert omnigent.calls == 1
    assert store.list_jobs()[0].state == "published"


@pytest.mark.asyncio
async def test_worker_publishes_complete_deterministic_review_body(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = FakeGitHub()

    assert await PollyWorker(store, github, FakeOmnigent()).run_once()

    assert (
        github.reviews[0]["body"]
        == """<!-- polly-review:acme/widgets#12:head -->
**Disposition:** BLOCKING

## Claude
Found one issue.

## Codex
Found one issue.

## Findings without a valid diff anchor
- **High · Unsafe branch** `app.py:2` — This branch can lose data.

**Approach:** Needs changes

## Blockers
- `app.py:2` **Unsafe branch** — This branch can lose data.

## Resolved previous findings
- fixed
- shared

## Still-open previous findings
- open"""
    )


@pytest.mark.asyncio
async def test_worker_runs_one_fresh_pair_per_durable_job_attempt(tmp_path: Path) -> None:
    class FlakyOmnigent(FakeOmnigent):
        async def review_pair(self, diff: str, review_id: str) -> dict[str, dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                raise ReviewerFailure("runner failed")
            return {
                "claude_review": _review(review_id, verdict="Mostly sound"),
                "codex_review": _review(review_id, verdict="Needs changes", priority="High"),
            }

    now = [0.0]
    database = tmp_path / "polly.db"
    store = PollyStore(database, clock=lambda: now[0])
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = FakeGitHub()
    omnigent = FlakyOmnigent()
    worker = PollyWorker(store, github, omnigent, max_job_attempts=2, retry_delays=(5,))

    assert await worker.run_once() is True
    assert omnigent.calls == 1
    assert store.list_jobs()[0].state == "retry_wait"
    del worker, store
    now[0] = 5
    store = PollyStore(database, clock=lambda: now[0])
    worker = PollyWorker(store, github, omnigent, max_job_attempts=2, retry_delays=(5,))
    assert await worker.run_once() is True
    assert omnigent.calls == 2
    assert store.list_jobs()[0].state == "published"


@pytest.mark.asyncio
async def test_worker_does_not_transport_retry_review_pair(tmp_path: Path) -> None:
    class OfflineOmnigent(FakeOmnigent):
        async def review_pair(self, diff: str, review_id: str) -> dict[str, dict[str, Any]]:
            self.calls += 1
            raise httpx.ReadTimeout("review transport failed")

    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    omnigent = OfflineOmnigent()

    assert await PollyWorker(
        store,
        FakeGitHub(),
        omnigent,
        transport_attempts=3,
        max_job_attempts=2,
        retry_delays=(5,),
    ).run_once()

    assert omnigent.calls == 1
    assert store.list_jobs()[0].state == "retry_wait"


@pytest.mark.asyncio
async def test_worker_transport_retries_are_due_and_finite(tmp_path: Path) -> None:
    class OfflineGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def find_review(
            self, owner: str, repo: str, number: int, marker: str
        ) -> dict[str, Any] | None:
            self.calls += 1
            raise httpx.ReadTimeout("offline")

    now = [0.0]
    store = PollyStore(tmp_path / "polly.db", clock=lambda: now[0])
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = OfflineGitHub()
    worker = PollyWorker(
        store,
        github,
        FakeOmnigent(),
        transport_attempts=1,
        max_job_attempts=3,
        retry_delays=(5, 10),
    )

    assert await worker.run_once() is True
    assert store.list_jobs()[0].state == "retry_wait"
    assert await worker.run_once() is False
    now[0] = 5
    assert await worker.run_once() is True
    assert store.list_jobs()[0].state == "retry_wait"
    now[0] = 15
    assert await worker.run_once() is True

    assert store.list_jobs()[0].state == "failed"
    assert await worker.run_once() is False
    assert github.calls == 3
    assert store.reset_failed(store.list_jobs()[0].id) is True
    assert store.list_jobs()[0].attempts == 0


@pytest.mark.parametrize("status_code", [429, 500, 503])
@pytest.mark.asyncio
async def test_worker_retryable_http_statuses_use_bounded_transport_and_job_retry(
    tmp_path: Path, status_code: int
) -> None:
    class UnavailableGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def find_review(
            self, owner: str, repo: str, number: int, marker: str
        ) -> dict[str, Any] | None:
            self.calls += 1
            response = httpx.Response(
                status_code,
                request=httpx.Request("GET", "https://api.github.test/reviews"),
            )
            response.raise_for_status()
            return None

    now = [0.0]
    store = PollyStore(tmp_path / "polly.db", clock=lambda: now[0])
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = UnavailableGitHub()
    worker = PollyWorker(
        store,
        github,
        FakeOmnigent(),
        transport_attempts=2,
        max_job_attempts=2,
        retry_delays=(5,),
        sleep=lambda _: asyncio.sleep(0),
    )

    assert await worker.run_once() is True
    assert store.list_jobs()[0].state == "retry_wait"
    assert github.calls == 2

    now[0] = 5
    assert await worker.run_once() is True
    assert store.list_jobs()[0].state == "failed"
    assert github.calls == 4


@pytest.mark.asyncio
async def test_worker_terminal_http_status_fails_without_retry(tmp_path: Path) -> None:
    class MissingGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def find_review(
            self, owner: str, repo: str, number: int, marker: str
        ) -> dict[str, Any] | None:
            self.calls += 1
            response = httpx.Response(
                404,
                request=httpx.Request("GET", "https://api.github.test/reviews"),
            )
            response.raise_for_status()
            return None

    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = MissingGitHub()

    assert await PollyWorker(store, github, FakeOmnigent(), transport_attempts=3).run_once()
    assert store.list_jobs()[0].state == "failed"
    assert github.calls == 1


@pytest.mark.asyncio
async def test_worker_crash_marker_replay_second_run_does_not_duplicate_review(
    tmp_path: Path,
) -> None:
    class UncertainGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.outage = True

        async def publish_review_once(
            self,
            owner: str,
            repo: str,
            number: int,
            head_sha: str,
            body: str,
            comments: list[dict[str, Any]],
        ) -> dict[str, Any]:
            if not self.reviews:
                self.reviews.append({"id": 1, "body": body, "comments": comments})
            if self.outage:
                raise httpx.ReadTimeout("response lost")
            return self.reviews[0]

    now = [0.0]
    store = PollyStore(tmp_path / "polly.db", clock=lambda: now[0])
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = UncertainGitHub()
    omnigent = FakeOmnigent()
    worker = PollyWorker(
        store,
        github,
        omnigent,
        transport_attempts=1,
        max_job_attempts=2,
        retry_delays=(1,),
    )

    assert await worker.run_once() is True
    assert store.list_jobs()[0].state == "retry_wait"
    github.outage = False
    now[0] = 1
    assert await worker.run_once() is True

    assert store.list_jobs()[0].state == "published"
    assert len(github.reviews) == 1
    assert omnigent.calls == 1


@pytest.mark.asyncio
async def test_worker_does_not_publish_when_head_changes_after_review(tmp_path: Path) -> None:
    class MovingHeadGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.head_reads = 0

        async def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
            self.head_reads += 1
            return {"head": {"sha": "head" if self.head_reads == 1 else "new-head"}}

    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)
    github = MovingHeadGitHub()

    assert await PollyWorker(store, github, FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("head", "superseded"),
        ("new-head", "pending"),
    ]
    assert github.reviews == []


@pytest.mark.asyncio
async def test_worker_marker_for_stale_head_restores_current_head(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("current", "acme", "widgets", 12, "current-head", 7)
    store.accept("stale", "acme", "widgets", 12, "stale-head", 7)
    github = FakeGitHub()
    github.head = "current-head"
    github.reviews.append(
        {"id": 1, "body": "<!-- polly-review:acme/widgets#12:stale-head -->", "comments": []}
    )

    assert await PollyWorker(store, github, FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("current-head", "pending"),
        ("stale-head", "superseded"),
    ]


@pytest.mark.parametrize("pull", [{}, {"head": {}}, {"head": {"sha": None}}, {"head": {"sha": 7}}])
@pytest.mark.asyncio
async def test_worker_fails_malformed_current_head_without_superseding(
    tmp_path: Path, pull: dict[str, Any]
) -> None:
    class MalformedHeadGitHub(FakeGitHub):
        async def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
            return pull

    store = PollyStore(tmp_path / "polly.db")
    store.accept("delivery", "acme", "widgets", 12, "head", 7)

    assert await PollyWorker(store, MalformedHeadGitHub(), FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [("head", "failed")]


@pytest.mark.asyncio
async def test_worker_restores_superseded_observed_current_head(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("current", "acme", "widgets", 12, "current-head", 7)
    store.accept("stale", "acme", "widgets", 12, "stale-head", 7)
    github = FakeGitHub()
    github.head = "current-head"

    assert await PollyWorker(store, github, FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("current-head", "pending"),
        ("stale-head", "superseded"),
    ]


@pytest.mark.asyncio
async def test_worker_creates_observed_current_head_missing_from_webhooks(tmp_path: Path) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("stale", "acme", "widgets", 12, "stale-head", 7)
    github = FakeGitHub()
    github.head = "current-head"

    assert await PollyWorker(store, github, FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("stale-head", "superseded"),
        ("current-head", "pending"),
    ]


@pytest.mark.parametrize("terminal_state", ["published", "failed"])
@pytest.mark.asyncio
async def test_worker_does_not_revive_terminal_observed_current_head(
    tmp_path: Path, terminal_state: str
) -> None:
    store = PollyStore(tmp_path / "polly.db")
    store.accept("current", "acme", "widgets", 12, "current-head", 7)
    current = store.list_jobs()[0]
    store.set_state(current.id, terminal_state)
    store.accept("stale", "acme", "widgets", 12, "stale-head", 7)
    github = FakeGitHub()
    github.head = "current-head"

    assert await PollyWorker(store, github, FakeOmnigent()).run_once() is True

    assert [(job.head_sha, job.state) for job in store.list_jobs()] == [
        ("current-head", terminal_state),
        ("stale-head", "superseded"),
    ]
