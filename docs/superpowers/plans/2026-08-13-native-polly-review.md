# Native Polly Review Implementation Plan

**Spec:** `docs/superpowers/specs/2026-08-13-polly-review-integration-design.md`

## Global Constraints

- Reuse Omnigent's device grant, secret store, `IntegrationDaemon`, session/file APIs, and CLI conventions.
- Add no runtime dependency; SQLite is stdlib.
- Use TDD for every behavior change.
- Keep the integration in Omnigent core under `omnigent/integrations/polly`.
- Never expose GitHub or Omnigent credentials to reviewer sessions.
- Do not commit, push, deploy, or change external services without explicit authorization.

## Task 1: OIDC scoped device authorization

**Files:** `omnigent/server/routes/device_auth.py`, `omnigent/server/app.py`, `tests/server/test_device_auth.py`

1. Replace the OIDC rejection test with a failing router/flow test proving an OIDC-authenticated browser identity can approve a device grant.
2. Verify the test fails because the router and app mount reject OIDC.
3. Generalize the existing router guard and identity lookup to Accounts plus OIDC without changing token scope, refresh, revocation, or header-mode rejection.
4. Mount the device router when either supported auth mode is active and device auth is enabled.
5. Run `uv run pytest tests/server/test_device_auth.py` and the affected server app tests.

## Task 2: Built-in safe review agent

**Files:** built-in agent bundle and server seeding tests identified from the current Polly seeding pattern.

1. Add failing tests for an idempotently seeded `polly-review` agent containing exactly `claude_review` and `codex_review`.
2. Assert both workers have fixed harnesses, no spawn, no skills, no OS environment, and no shell/write tools.
3. Add the smallest bundle and reuse the existing content-aware seed helper.
4. Run the focused bundle/server tests.

## Task 3: Durable native integration runtime

**Files:** `omnigent/integrations/polly/*` plus focused tests under `tests/integrations/polly/`.

Build in red/green slices:

1. Webhook HMAC verification, event/action filtering, draft/fork skips, and delivery deduplication.
2. SQLite schema and job transitions: pending, running, retry_wait, published, failed, superseded; recover running jobs at startup.
3. GitHub App JWT, installation-token cache/refresh, repository installation resolution, diff fetch, review lookup by hidden marker, one review publish, and living status comment update.
4. Omnigent client using scoped access/refresh tokens: create parent and exact reviewer child sessions, upload/copy one diff file, start turns, poll durable results, and interrupt the surviving child if its peer fails.
5. Strict Review-ID-scoped output extraction and deterministic merge: blockers union, worst verdict, labeled summaries, path/line dedupe with higher priority, resolved-ID intersection, still-open union, and invalid anchors moved to the body.
6. Worker orchestration: current-head checks before and after review, one fresh reviewer pair per durable job attempt, bounded idempotent transport retry, crash recovery, supersession, and marker-based exactly-once publication recovery.
7. End-to-end test with fake GitHub and Omnigent proving two child outputs create one GitHub review.

Run focused tests after every slice, then the complete Polly integration suite.

## Task 4: CLI setup, onboarding, and cutover support

**Files:** `omnigent/cli.py`, Polly integration CLI/runtime modules, and CLI tests.

1. Add failing CLI tests for setup, foreground/background, status, stop, logs, doctor, and retry.
2. Reuse `IntegrationDaemon("polly", ...)` for lifecycle behavior.
3. Implement non-secret config under the Omnigent data directory and store the GitHub private key, webhook secret, and Omnigent refresh token through the existing secret store.
4. Implement GitHub App manifest onboarding with CSRF state and one-time code exchange, plus existing-App import using a private-key file and hidden webhook-secret prompt.
5. Make setup select and validate the `polly-review` agent, one online host with ready Claude and Codex harnesses, and a dedicated empty absolute workspace.
6. Implement `doctor` as read-only checks and `retry` as a failed-job reset.
7. Add migration guidance to the existing integration documentation: canary, restart recovery, delivery replay, head supersession, drain old service, change one webhook URL, and preserve the old service only for rollback.
8. Run focused CLI tests, `pre-commit run --all-files`, and the full relevant Python suite.

## Acceptance

- A valid webhook is durably acknowledged before review work starts.
- Two child sessions are visible in Omnigent and exactly one review appears on GitHub.
- Replaying a delivery or losing a GitHub POST response never creates a duplicate review.
- Restarting the daemon resumes accepted work.
- A newer head prevents an older review from publishing.
- Setup and logs never expose stored secrets.
- Accounts behavior remains unchanged; OIDC works; header/proxy remains rejected.
