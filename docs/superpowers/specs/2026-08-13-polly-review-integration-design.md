# Polly Review Integration Design

## Goal

Make Polly Review a first-class Omnigent integration that an operator can connect to their own GitHub App and Omnigent deployment without Kainotomic-specific infrastructure.

## Contract

- Accept only GitHub `pull_request` webhooks for opened, reopened, synchronize, and ready-for-review actions.
- Skip drafts and forks.
- Run exactly two deterministic reviewers: Claude and Codex.
- Give both reviewers the same uploaded diff without cloning or shell access.
- Strictly validate Review-ID-scoped JSON and merge the two results deterministically.
- Publish exactly one GitHub review per repository, pull request, and head SHA.
- Persist accepted deliveries and jobs so restarts and webhook redelivery do not lose or duplicate reviews.

## Security

- Operators own and install their GitHub App.
- The App requests only Pull requests: write and the pull_request event; Metadata read is implicit.
- Omnigent access uses the existing scoped device grant under Accounts or OIDC. Header/proxy auth remains unsupported.
- Secrets use Omnigent's existing keychain/owner-only fallback store. No PATs, cookie secrets, or administrator JWT minting.
- Reviewers have no spawn, skills, OS environment, shell, repository checkout, GitHub credentials, or write tools.
- Reviews run from a dedicated empty workspace, never `/`, a home directory, or `/root`.

## Runtime

`omni integration polly` reuses `IntegrationDaemon` and runs a single worker backed by stdlib SQLite in WAL mode. Webhook receipt validates and persists before returning HTTP 202. Interrupted running jobs return to pending at startup. A newer PR head supersedes older pending work.

The worker uploads the capped UTF-8 diff once per durable job attempt, creates one direct Claude and Codex child-session pair, polls both, and then merges their strict outputs. A failed pair consumes that durable attempt; the next scheduled attempt creates one fresh pair. Before publication it rechecks the PR head. A hidden deterministic marker in the review body lets retries recover an uncertain GitHub POST without creating a duplicate.

## CLI

```text
omni integration polly setup --server URL --public-url HTTPS_URL
omni integration polly [--background]
omni integration polly status
omni integration polly stop
omni integration polly logs [-f]
omni integration polly doctor
omni integration polly retry JOB_ID
```

Setup supports GitHub's App manifest flow and import of an existing App. `doctor` validates Omnigent authentication, the bundled agent, host harness readiness, the dedicated workspace, App installation, selected repositories, and webhook reachability without starting a review.

## Deliberate Limits

- Pull requests only; no push or standalone commit reviews.
- One daemon per configuration; no multi-replica coordination.
- Diff-only review; no repository checkout.
- No smart routing; Claude-plus-Codex fanout is fixed.
- Existing Node webhook state is not migrated. Cutover drains and stops it before changing the App webhook URL.

## Migration and rollback

Canary the native integration with a test repository first. Verify restart recovery by
stopping it after a delivery is accepted, restarting it, and confirming the job resumes;
then replay that delivery and confirm no second review appears. Push a newer PR head while
an older review is running and confirm the older job is superseded before publication.

For production cutover, drain and stop the old service, then change the GitHub App's single
webhook URL to the native Polly `/webhooks/github` endpoint. Keep the stopped old service and
its data only as a rollback target; never run both consumers against the same App webhook.
