"""E2E: the session header names the repo and branch the session works in.

The header's title bar reads the default environment's ``metadata.git``, which
the runner derives from the workspace's ``.git`` state. This drives the real
SPA against the real runner and asserts the two agree: whatever repo and ref
the API reports must be what the header renders.

The e2e runner's workspace is this repository, so the assertion is written
against the live API response rather than a hardcoded name — it holds on any
branch, in a worktree, and in CI.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def workspace_git(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str, dict[str, object]]]:
    """Read the session's reported git identity, skipping outside a repo.

    :param seeded_session: ``(base_url, session_id)`` from the shared fixture.
    :returns: ``(base_url, session_id, git)`` where ``git`` is the
        environment's ``metadata.git`` block.
    """
    base_url, session_id = seeded_session
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default",
        timeout=10.0,
    )
    resp.raise_for_status()
    git = resp.json().get("metadata", {}).get("git")
    if not git:
        pytest.skip("runner workspace is not a git checkout; nothing for the title bar to show")
    yield (base_url, session_id, git)


def test_header_names_the_repo_and_ref_the_session_works_in(
    page: Page,
    workspace_git: tuple[str, str, dict[str, object]],
) -> None:
    """The title bar renders the repo and ref the runner reports for the workspace."""
    base_url, session_id, git = workspace_git
    repo = str(git["repo"])
    ref = git["ref"]
    page.goto(f"{base_url}/c/{session_id}")

    identity = page.get_by_test_id("workspace-identity")
    expect(identity).to_be_visible(timeout=30_000)
    # The repo name is the part a user scans for when several sessions are
    # open; a miss here means the runner's git block never reached the header.
    expect(identity).to_contain_text(repo)
    if ref is not None:
        expect(identity).to_contain_text(str(ref))
    if git.get("worktree"):
        # The only on-screen difference between two sessions on the same repo.
        expect(identity).to_contain_text("worktree")
