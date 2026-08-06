"""UI check: a session's harness picks its branded icon in the Agents rail.

The rail's ``iconForWrapperOrHarness`` (``shell/SubagentsPanel.tsx``) resolves a
row's glyph from the session's native-wrapper ``iconKind`` when it has one, and
otherwise from the harness string — falling back to a generic lucide bot. That
fallback is silent: a missing mapping looks exactly like a deliberate one, so
the mapping is only observable by rendering a row and reading which glyph came
out.

Grok Build is the case under test. It is a first-class ACP harness with no
native wrapper, so the harness string is the *only* thing that can name it, and
before its mapping existed a Grok session rendered as the generic bot —
indistinguishable from any unmapped harness.

The rail is deliberately the surface here rather than the Add-agent picker.
``AgentCard`` is only reached from ``AddAgentDialog``, which never calls
``prefetchAvailableAgentDetails``, so a session-discovered agent's harness stays
``null`` there (``sessionAgentFromScan``) and *every* harness renders as the bot
regardless of mapping. The rail reads the session's own harness, so it is where
this behaviour is actually observable.

No LLM turn and no Grok binary are involved: the session is created with
``harness_override`` and never driven, so this stays fast and deterministic.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

# First subpath of each glyph, enough to tell them apart. GrokIcon is the
# hand-authored mark in ``web/src/components/icons/GrokIcon.tsx``; the fallback
# is lucide's ``bot``, whose first path is the antenna stem.
_GROK_FIRST_PATH = re.compile(r"^M6\.6 18")
_BOT_FIRST_PATH = re.compile(r"^M12 8V4H8")

_MAIN_ROW = '[data-testid="subagent-main-row"]'


def _session_harness(base_url: str, session_id: str) -> str | None:
    """Return the harness the server records for *session_id*."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("harness")


def _open_agents_rail(page: Page) -> None:
    """Open the workspace rail and select its Agents tab."""
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()


def test_grok_session_row_uses_the_grok_glyph(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A ``grok`` session's rail row renders the Grok mark, not the bot fallback."""
    base_url, session_id = seeded_session

    # Baseline on the seeded session first: its harness (openai-agents) has no
    # mapping, so it must render the bot. Without this the test would pass on a
    # build that handed every row the Grok glyph.
    assert _session_harness(base_url, session_id) != "grok"
    page.goto(f"{base_url}/c/{session_id}")
    _open_agents_rail(page)
    expect(page.locator(_MAIN_ROW).locator("svg path").first).to_have_attribute(
        "d", _BOT_FIRST_PATH, timeout=30_000
    )

    # Now the same row for a grok session. harness_override is enough — the row
    # reads the session's harness, so no turn is run and no Grok CLI is needed.
    agent_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent_resp.raise_for_status()
    create = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_resp.json()["id"], "harness_override": "grok"},
        timeout=15.0,
    )
    create.raise_for_status()
    grok_session_id = create.json()["id"]
    assert _session_harness(base_url, grok_session_id) == "grok", (
        "harness_override did not persist as grok"
    )

    page.goto(f"{base_url}/c/{grok_session_id}")
    _open_agents_rail(page)
    expect(page.locator(_MAIN_ROW).locator("svg path").first).to_have_attribute(
        "d", _GROK_FIRST_PATH, timeout=30_000
    )
