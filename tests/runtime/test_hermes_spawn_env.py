"""
Tests for ``_build_hermes_spawn_env`` in ``omnigent/runtime/workflow.py`` and the
end-to-end model-override wiring for the ``hermes`` harness.

The spawn-env builder maps ``spec.executor`` fields to the ``HARNESS_HERMES_*``
env vars the hermes harness wrap reads at executor-construction time; the executor
then forwards ``HARNESS_HERMES_MODEL`` to the CLI as ``hermes chat -m <model>``
(covered in ``tests/inner/test_hermes_executor.py``). These are unit tests — no
subprocess spawn, no real hermes CLI.

Mirrors ``test_pi_spawn_env.py`` / the kimi sibling: hermes is a CLI-subprocess,
OWN_AUTH harness, so — unlike the gateway harnesses — the builder threads only the
model, working directory, and ``os_env`` sandbox spec (provider/credentials live in
``~/.hermes``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harness_plugins import model_env_keys
from omnigent.runtime.workflow import _build_hermes_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OMNIGENT_CONFIG_HOME at an empty temp dir so the developer's real
    global config can't hijack the model resolution under test."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(*, model: str | None = None) -> AgentSpec:
    """Build a minimal ``hermes`` :class:`AgentSpec` for spawn-env tests."""
    config: dict[str, object] = {"harness": "hermes"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="test-hermes",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_hermes_registered_in_model_env_keys() -> None:
    """``hermes`` maps to ``HARNESS_HERMES_MODEL`` so ``args.model`` /
    ``executor.model`` (and a per-session ``/model`` override) can reach it.

    ``hermes-native`` is intentionally NOT here — it takes the model as a ``-m``
    argv at TUI launch, like the other native CLIs.
    """
    keys = model_env_keys()
    assert keys.get("hermes") == "HARNESS_HERMES_MODEL"
    assert "hermes-native" not in keys


def test_hermes_spawn_env_threads_model() -> None:
    """A spec model lands in ``HARNESS_HERMES_MODEL`` — the var the executor
    forwards to the CLI as ``hermes chat -m <model>``."""
    env = _build_hermes_spawn_env(_make_spec(model="anthropic/claude-sonnet-4"))
    assert env["HARNESS_HERMES_MODEL"] == "anthropic/claude-sonnet-4"


def test_hermes_spawn_env_omits_model_when_unset() -> None:
    """No spec model → no ``HARNESS_HERMES_MODEL`` key, so Hermes falls back to
    its own configured default rather than an empty override."""
    env = _build_hermes_spawn_env(_make_spec(model=None))
    assert "HARNESS_HERMES_MODEL" not in env


def test_hermes_spawn_env_threads_cwd() -> None:
    """The session workspace is threaded as ``HARNESS_HERMES_CWD`` so Hermes'
    tools operate on the user's project, not the runner cwd."""
    workspace = Path("/tmp/repo-under-test")
    env = _build_hermes_spawn_env(_make_spec(), cwd=workspace)
    assert env["HARNESS_HERMES_CWD"] == str(workspace)


def test_hermes_spawn_env_serializes_os_env() -> None:
    """``spec.os_env`` is serialized into ``HARNESS_HERMES_OS_ENV`` as JSON the
    executor decodes back into an ``OSEnvSpec``."""
    from omnigent.spec.types import OSEnvSpec

    spec = _make_spec(model="gpt-5.5")
    spec = AgentSpec(
        spec_version=spec.spec_version,
        name=spec.name,
        instructions=spec.instructions,
        executor=spec.executor,
        llm=spec.llm,
        os_env=OSEnvSpec(type="caller_process"),
    )
    env = _build_hermes_spawn_env(spec)
    payload = json.loads(env["HARNESS_HERMES_OS_ENV"])
    assert payload["type"] == "caller_process"


def test_build_spawn_env_from_spec_applies_model_override_for_hermes() -> None:
    """A per-session ``/model`` override wins over the spec model on the hermes
    path — proving ``_build_spawn_env_from_spec`` returns a real env for hermes
    (not ``None``) and applies the override into ``HARNESS_HERMES_MODEL``.

    This is the regression guard for the whole point of the change: before
    registering hermes, ``_build_spawn_env_from_spec`` returned ``None`` for it
    and the override silently dropped.
    """
    from omnigent.runner.app import _build_spawn_env_from_spec

    env = _build_spawn_env_from_spec(
        _make_spec(model="gpt-5.5"),
        "hermes",
        model_override="anthropic/claude-sonnet-4",
    )
    assert env is not None
    assert env["HARNESS_HERMES_MODEL"] == "anthropic/claude-sonnet-4"
