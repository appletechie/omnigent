"""``harness: grok`` — the builtin Grok Build ACP harness."""

from __future__ import annotations

import omnigent.inner.grok_harness as gh
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import (
    harness_capabilities,
    harness_labels,
    harness_modules,
    valid_harnesses,
)
from omnigent.inner.acp_executor import AcpExecutor
from omnigent.onboarding.harness_install import GROK_KEY
from omnigent.onboarding.harness_readiness import harness_is_configured


def test_grok_registered_as_builtin_harness() -> None:
    assert "grok" in valid_harnesses()
    assert harness_modules()["grok"] == "omnigent.inner.grok_harness"
    assert canonicalize_harness("grok-build") == "grok"
    assert harness_labels().get("grok") == "Grok Build"
    assert "grok" in harness_capabilities()


def test_grok_executor_drives_grok_agent_stdio(monkeypatch) -> None:
    # No OMNIGENT_GROK_PATH / grok on PATH -> command falls back to bare "grok".
    monkeypatch.delenv("OMNIGENT_GROK_PATH", raising=False)
    monkeypatch.delenv("HARNESS_GROK_CWD", raising=False)
    ex = gh._build_grok_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.command.endswith("grok agent stdio")
    assert ex._config.name == "Grok Build"


def test_grok_readiness_gates_on_binary(monkeypatch) -> None:
    import omnigent.onboarding.harness_readiness as hr

    monkeypatch.setattr(hr, "harness_cli_installed", lambda key: key == GROK_KEY)
    assert harness_is_configured("grok") is True
    monkeypatch.setattr(hr, "harness_cli_installed", lambda key: False)
    assert harness_is_configured("grok") is False
