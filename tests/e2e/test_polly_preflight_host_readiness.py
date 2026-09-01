"""Polly's roster preflight must gate workers on host harness readiness.

Bug: a ``command -v`` shell probe only checks the runner shell PATH, so it
marks an installed-but-unauthenticated harness (e.g. OpenCode reporting
``needs-auth``) as available; Polly then dispatches to a worker that cannot
boot. The authoritative source is the bound host's ``configured_harnesses``
readiness map (surfaced via ``sys_session_get_info``), where a worker is truly
available only when its readiness value is exactly ``true``.

These tests pin the preflight contract in ``examples/polly/config.yaml``:
readiness-map first, ``command -v`` only as the explicit fallback for the
no-bound-host / lookup-failed case.

Run::

    pytest tests/e2e/test_polly_preflight_host_readiness.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/e2e/<this file> -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"


def _polly_prompt() -> str:
    """Return the raw prompt string from ``examples/polly/config.yaml``."""
    cfg = yaml.safe_load((_POLLY / "config.yaml").read_text(encoding="utf-8"))
    return cfg.get("prompt", "")


def test_polly_preflight_references_host_readiness() -> None:
    """The preflight must consult ``configured_harnesses``, not only ``command -v``.

    ``command -v`` checks the runner shell PATH only. It cannot distinguish:

    - a harness installed but unauthenticated (readiness ``"needs-auth"``)
    - a harness available via the Omnigent resolver but absent from PATH
    - a harness that fails the version check (``"version-too-low"``)

    The prompt must instruct the brain to read the host readiness map so Polly
    never dispatches to a worker that cannot boot.
    """
    prompt = _polly_prompt()
    assert "configured_harnesses" in prompt, (
        "Polly's roster preflight must consult the bound host's "
        "`configured_harnesses` readiness map (via sys_session_get_info); a "
        "shell `command -v` probe alone cannot distinguish "
        "installed-but-unauthenticated workers from truly available ones."
    )
    assert "sys_session_get_info" in prompt, (
        "The preflight must name the tool that surfaces the readiness map "
        "(sys_session_get_info) so the brain knows how to fetch it."
    )


def test_polly_preflight_uses_readiness_true_filter() -> None:
    """The prompt must require readiness exactly ``true`` to route to a worker.

    The readiness map carries non-true values (``"needs-auth"``,
    ``"binary-missing"``, ``"version-too-low"``, ``false``); all must be
    treated as unavailable.
    """
    prompt = _polly_prompt()
    lp = prompt.lower()
    assert "configured_harnesses" in prompt and "exactly `true`" in lp, (
        "The polly prompt must require readiness == true when filtering "
        "workers from the configured_harnesses map; values like 'needs-auth', "
        "'binary-missing', and 'version-too-low' must all be treated as "
        "unavailable."
    )
    # Non-true readiness values must be spelled out as unavailable states.
    for state in ("needs-auth", "binary-missing", "version-too-low"):
        assert state in lp, (
            f"The preflight paragraph should name {state!r} as an unavailable "
            "readiness state so the brain treats it as not routable."
        )


def test_polly_preflight_readiness_map_is_primary_gate() -> None:
    """The readiness map is the primary gate; ``command -v`` only the fallback.

    A prompt that leads with a PATH probe reintroduces the bug: the shell
    check would win before the readiness map is consulted. The readiness-map
    instruction must come first, and any ``command -v`` mention must be
    scoped to the no-bound-host / lookup-failed fallback.
    """
    prompt = _polly_prompt()
    readiness_pos = prompt.find("configured_harnesses")
    command_v_pos = prompt.find("command -v")
    assert readiness_pos != -1, "prompt must reference configured_harnesses"
    if command_v_pos != -1:
        assert readiness_pos < command_v_pos, (
            "The configured_harnesses readiness check must come before any "
            "`command -v` mention: the shell probe is only the fallback for "
            "sessions with no bound host, never the primary availability gate."
        )
        # The fallback must be explicitly conditioned on the readiness map
        # being unavailable, not offered as an equal alternative.
        window = " ".join(prompt[readiness_pos:command_v_pos].lower().split())
        assert "absent" in window or "null" in window or "fall back" in window, (
            "The `command -v` probe must be framed as the fallback for an "
            "absent/null configured_harnesses map, not as a primary check."
        )


def test_polly_preflight_defines_no_host_fallback() -> None:
    """The prompt must define behavior when ``configured_harnesses`` is null.

    ``host_id`` is legitimately ``None`` for CLI-initiated sessions and
    caller-managed runners, and the host lookup is best-effort — in both
    cases ``configured_harnesses`` is ``null``. Without an explicit fallback
    the 'exactly true' rule matches nothing and Polly would treat every
    worker as unavailable, stalling all dispatch.
    """
    # Normalize whitespace: YAML block-scalar line wraps can split phrases.
    lp = " ".join(_polly_prompt().lower().split())
    assert ("absent" in lp or "null" in lp) and "fall back" in lp, (
        "The preflight must state what to do when configured_harnesses is "
        "absent/null (no bound host, or the readiness lookup failed): fall "
        "back to the shell probe rather than concluding no worker is "
        "available."
    )
