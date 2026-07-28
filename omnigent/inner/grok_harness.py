"""``harness: grok`` wrap — Grok Build (xAI) as a first-class ACP harness.

Thin module exposing :func:`create_app` — the entry point the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"grok"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Grok Build speaks the Agent Client Protocol, so this reuses the generic
:class:`omnigent.inner.acp_executor.AcpExecutor` with a fixed
``grok agent stdio`` command — the same executor a user-configured ``acp:``
agent uses, but promoted to a builtin harness with lifecycle detection, an
``omnigent grok`` wrapper, and setup-menu awareness (see the ``qwen`` pattern).

Auth is Grok's own (``grok login`` xAI OAuth or ``XAI_API_KEY``); Omnigent stores
no credential. Model selection is likewise the CLI's own (``/model`` inside
Grok) — this harness declares no Omnigent-driven model override.

Env vars read at startup:

- ``HARNESS_GROK_CWD``: working directory for the grok subprocess. ``None``
  falls back to ``OMNIGENT_RUNNER_WORKSPACE`` then the inherited cwd.
- ``OMNIGENT_GROK_PATH``: absolute path to a ``grok`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_GROK_OS_ENV``: JSON-encoded :class:`OSEnvSpec`. When unset, falls
  back to ``caller_process`` + ``sandbox=none``.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

_ENV_CWD = "HARNESS_GROK_CWD"
_ENV_OS_ENV = "HARNESS_GROK_OS_ENV"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from env config.

    Decodes the JSON-encoded :data:`_ENV_OS_ENV`; falls back to
    ``caller_process`` + ``sandbox=none`` when the var is missing or malformed.
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env", _ENV_OS_ENV, exc
            )
            payload = None
        if isinstance(payload, dict):
            sandbox_payload = payload.get("sandbox")
            sandbox = (
                OSEnvSandboxSpec(**sandbox_payload) if isinstance(sandbox_payload, dict) else None
            )
            return OSEnvSpec(
                type=str(payload.get("type", "caller_process")),
                cwd=payload.get("cwd"),
                sandbox=sandbox,
                fork=bool(payload.get("fork", False)),
            )
    return OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )


def _build_grok_executor() -> Executor:
    """Construct an :class:`AcpExecutor` driving ``grok agent stdio`` (lazily)."""
    cwd_raw = os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE")
    grok_path = resolve_harness_path("grok") or "grok"
    config = AcpAgentConfig(command=f"{grok_path} agent stdio", name="Grok Build")
    return AcpExecutor(config=config, cwd=cwd_raw or None, os_env=_resolve_os_env())


def create_app() -> FastAPI:
    """Build the grok harness's FastAPI app (required entry point).

    The wrapped :class:`AcpExecutor` is constructed lazily on the first turn, so
    an absent ``grok`` CLI surfaces as a request-time error rather than an
    app-boot crash.
    """
    adapter = ExecutorAdapter(executor_factory=_build_grok_executor, harness_label="Grok Build")
    return adapter.build()
