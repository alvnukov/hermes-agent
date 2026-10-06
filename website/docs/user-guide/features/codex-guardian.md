---
title: Codex Guardian
description: Experimental account-bound Codex Auto-review at Hermes approval gates.
sidebar_label: Codex Guardian
---

# Codex Guardian (experimental)

Guardian reviews an action at the existing smart approval gate using the ChatGPT
subscription account that produced it. It uses the real Codex reviewer protocol,
not Hermes's ordinary auxiliary-model safety classifier. The integration is off
by default. Existing Codex OAuth accounts work without another login or a second
credential store. Hermes keeps its direct Responses runtime.

## Enable and disable

In Desktop, open **Settings → Providers**, expand the connected ChatGPT/Codex
account, and check **Codex Guardian (experimental)**. Each account has its own
checkbox. A checked originating account uses Guardian; an unchecked originating
account uses the existing Hermes approval backend. Checking one account does not
check other accounts. Shared profile assignments retain their own choice.

Use `approvals.mode: smart` to reach automatic review. `manual` still requires
human confirmation. Administrative deny rules and other independent approval
restrictions continue to apply. Guardian never creates a session or prefix grant.

Optional settings in the active profile's `config.yaml`:

```yaml
approvals:
  mode: smart
  backend: legacy
  codex_guardian:
    timeout: 20
    max_retries: 1
    max_context_bytes: 65536
    model: null
    reasoning_effort: null
```

`backend` accepts `legacy` or `codex_guardian`. The originating Codex account's
checkbox takes precedence, including an unchecked account under a global
`codex_guardian` setting. A globally selected Guardian gate without compatible
OAuth provenance requires manual confirmation. There is no separate reviewer
account setting. Timeout is finite, positive and at most 300 seconds; retries are
0–2; context is 1024–262144 bytes. Malformed settings cannot produce an ALLOW.

Leave `model` and `reasoning_effort` unset to select the supported defaults.
Only `codex-auto-review` is accepted as the reviewer model. Hermes queries the
live catalog through the pinned account: prefer `low` only when advertised,
otherwise use that model's advertised default. An unsupported effort, absent
model or parent model override to an ordinary model requires manual confirmation.
Hermes never falls back to the parent model, an API key, another provider or
another account for this review.

A catalog requiring Codex's shared sandbox, or supplying a reviewer policy or
template that differs from the pinned supported resources, also requires manual
confirmation. Hermes does not silently ignore that changed server requirement.

## Origin, credentials and execution

```text
main physical OAuth request (account A)
  → completed response R1 and tool call T1
  → immutable ReviewBinding(A, workspace, grant generation, R1, T1, args, policy)
  → current credentials for that exact persisted assignment/owner
  → Guardian HTTP/SSE review through A, with parent_response_id=R1
  → ALLOW receipt for T1, or DENY / ESCALATE
  → revalidate current account, cancellation, policy, arguments, cwd and resources
  → consume receipt once immediately before the execution boundary
```

The binding contains no access or refresh token. Stable entry IDs, owner subject,
workspace, login generation, profile/store scopes, route/residency and control
generations distinguish identity from aliases or list positions. Policy versions
also fingerprint the actual bundled policy/template/contract bytes. The actual
client token determines the originating entry; neither the active UI account nor
the pool cursor determines the reviewer. A same-account refresh uses the existing
auth locks and preserves identity. Reauthorization, changed workspace, logout,
deletion, disable/re-enable or checkbox off/on invalidates pending reviews.
Shared accounts also verify the canonical owner's control generation.

Each physical retry gets new provenance. `response.created` supplies its ID when
the terminal event omits it, but a partial stream is insufficient. A retry's tool
call cannot inherit the preceding attempt's ID. Missing/ambiguous provenance,
middleware replies without a physical request, cancellation, retired turns and
late responses require manual confirmation. Concurrent sessions keep independent
bindings and reviewer clients; capability results are not shared across accounts.

## Protocol and context

Eligible checked-account main requests merge
`client_metadata.guardian_credits_requested="true"`. Reserved reviewer metadata
and effective SDK default reviewer headers are removed locally; shared client
defaults are not mutated. An unchecked account's ordinary request is unchanged.

Reviewer requests use the trusted HTTPS Codex OAuth base
`https://chatgpt.com/backend-api/codex`, endpoint `/responses`, existing bearer and
workspace headers, and honest Hermes originator. Redirects are disabled. Headers
include `x-codex-guardian: reviewer` and `x-openai-subagent: guardian`;
`client_metadata` contains `parent_response_id` and `x-openai-subagent=guardian`.
The reviewer receives no main credit request, `previous_response_id`, inherited
service tier or routing hint. It uses a fresh request-local HTTP/SSE client, no
authenticated main/reviewer WebSocket reuse.

When the live catalog advertises Responses Lite, Hermes sends its upstream header,
developer policy in `input`, `reasoning.context=all_turns`, and omits empty tools.
The copied upstream Guardian policy/template are developer-scoped. Local approval
policy is appended as trusted policy. User goals, conversation history, tool
output and exact action details are explicit untrusted evidence. The action
includes cwd, local environment/permission limits and bounded resource evidence.
The entire captured transcript and action must fit the context cap; essential
arguments are never silently truncated. If secret redaction would change this
evidence, review escalates without sending it.

The output schema has `strict: false`, no extra properties, and requires only
`outcome: allow|deny`. Omitted risk defaults to low for allow/high for deny;
authorization defaults to unknown. Hermes intentionally parses more strictly than
upstream: full JSON only, no prose wrappers, duplicate/unknown fields, nulls or
non-JSON constants. Rationale stays private and is never logged or used as an
authorization heuristic.

## Coverage and limitations

The adapter covers existing smart gates for local `terminal` foreground/background
execution and fresh/reset project-mode local `execute_code`. Reused Python state
requires manual confirmation because a prior cell can change callable bindings.
It rechecks receipts at the shell
execution, background spawn and Python cell-write boundaries. Other sensitive
tools retain their existing checks; this does not claim protection of every tool.
Nested Python RPC actions have no originating model tool-call binding and require
the existing manual route when they encounter a Guardian gate.

There are no reviewer investigation tools. Bounded literal file paths, script
content and directory descendants are captured and rechecked. Unresolved shell
control flow/expansion, nested shell execution, unsupported script languages,
dynamic Python calls/imports and remote/strict execution environments require
manual confirmation. Large resource trees also escalate. These snapshots reduce
changes between review and execution; they are not an OS sandbox or a filesystem
transaction, and cannot eliminate a concurrent write after the final check.

DENY blocks automatic execution; a permitted explicit human override uses the
existing interface. ESCALATE also uses that interface. Without a human approval
surface, an action requiring confirmation is rejected. Timeouts, auth errors,
rate limits, server errors, invalid verdicts and unavailable capabilities never
become ALLOW. Only bounded 429/5xx retries within the same account and total
deadline are possible; `Retry-After` is respected.

Diagnostics contain a redacted account alias, outcome, reason code, latency and
attempt correlation. Reported reviewer token counts are recorded separately in
`_guardian_usage` and diagnostic logs, including completed malformed verdicts.
Missing usage remains missing. Main generation totals and monetary estimates are
not rewritten. Tokens, Authorization, full prompts and private rationale are not
logged.

## Evidence and opt-in smoke test

Implementation research pins **openai/codex
`8e23d1836fd22d1c920533f39851db1c7d795744`**. Relevant sources:

- [Client markers, Lite requests and routing](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/core/src/client.rs)
- [Reviewer model and effort selection](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/ext/guardian-reviewer/src/model.rs)
- [Assessment schema and defaults](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/ext/guardian-reviewer/src/assessment.rs)
- [Context construction](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/core/src/guardian/prompt.rs)
- [Policy template](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/prompts/templates/guardian/policy_template.md)
- [Network tests, including inherited HTTP fallback](https://github.com/openai/codex/blob/8e23d1836fd22d1c920533f39851db1c7d795744/codex-rs/core/tests/suite/guardian_review.rs)

Policy resources retain upstream Apache-2.0 LICENSE, NOTICE and attribution in
`agent/guardian_resources/`. Hermes appends an explicit environment override: it
does not claim Codex's shared read-only sandbox or investigation tools. Hermes
also rejects upstream's ordinary-model fallback.

Automated tests use fake credential stores, the real SDK with mock HTTP/SSE, and
mock execution boundaries. They test ancestry, refresh/control races, verdict
failures and absence of side effects. They do not contact a subscription account.

Run a real smoke check only by explicit choice of an enabled connected entry:

```bash
python scripts/guardian_smoke.py --live --account EXACT_ENTRY_ID --model MAIN_CODEX_MODEL
```

It obtains one real synthetic `pwd` proposal and reviews that real response's ID;
it never executes the proposal or iterates through accounts. It reports main and
review usage separately and keeps billing status `unknown`. Its two model requests
may consume usage. The integration was not live-tested during implementation.

Source code and mocks establish client protocol and local enforcement. They do
not prove server acceptance, account eligibility, parent ID expiry/ancestry rules,
future compatibility or free credit treatment. Even a successful live verdict
does not establish absence of charges. Ordinary Responses applications do not
automatically inherit Codex Auto-review; see the
[official guardrails and approvals guide](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals).

## Implementation map and verification

The feature lives in these files and seams:

| Area | Files |
| --- | --- |
| Protocol, policy, HTTP/SSE and logging | `agent/guardian_protocol.py`, `guardian_transport.py`, `guardian_logging.py`, `guardian_resources/*` |
| Immutable origin and local action receipts | `agent/guardian_provenance.py`, `guardian_credentials.py`, `guardian_action.py`, `guardian_review.py` |
| Main wire and accepted responses | `agent/codex_runtime.py`, `turn_response_intake.py` |
| Account lifecycle and persistence | `agent/credential_accounts.py`, `credential_pool.py`, `credential_pool_admin.py`, `credential_pool_persistence.py`; `hermes_cli/auth.py`, `auth_codex.py`, `auth_pool.py` |
| Approval gates and shared dispatch | `agent/tool_executor.py`, `terminal_approval_batch.py`; `tools/approval.py`, `approval_guardian.py`, `approval_command_findings.py` |
| Final execution boundaries | `tools/terminal_tool.py`, `terminal_tool_foreground.py`, `terminal_tool_background.py`, `code_kernel.py` |
| Profile settings and strict account API | `hermes_cli/guardian_config.py`, `approval_defaults.py`, `config_validation_general.py`, `config.py`, `config_defaults.py`, `web_models.py` |
| Desktop checkbox | `apps/desktop/src/app/settings/provider-guardian-checkbox.tsx`, `providers-settings.tsx`, `api/config.ts`, `types/hermes.ts`, provider locale files |
| Packaging and opt-in check | `pyproject.toml`, `scripts/guardian_smoke.py` |
| Tests | `tests/agent/test_guardian_*.py`, `tests/tools/test_guardian_*.py`, `tests/hermes_cli/test_guardian_config.py`, `test_web_codex_accounts.py`, `tests/scripts/test_guardian_smoke.py`, Desktop provider settings tests |

On 2026-10-06, the canonical `scripts/run_tests.sh` runner verified the combined
27-file Guardian/approval/Codex regression selection: **653 passed, 0 failed,
1 skipped** (a Linux-only case on macOS). A subsequent seven-file run including
the final added failure/executor/policy tests passed **96 tests**. These selections
overlap; their counts should not be added. Additional account/auth regression
selections passed; a broader existing credential suite had six failures in
`test_credential_pool_anthropic_refresh_race.py` and
`test_credential_pool_seed_existing_env_sources.py`, reproduced unchanged on the
base `main` commit `069a4d4e79d7c77589d0c2920eea8db8c2fa2fcb`.

All 11 checks in `python scripts/check` passed; new adapter/config/smoke files
passed `ty check` with the project test interpreter. Desktop provider UI tests
passed **28 tests**; renderer, Electron, E2E and builder TypeScript checks and
targeted ESLint passed. The pinned bundled Codex catalog also selects the supported
reviewer locally. Its template omits an optional extra-policy slot; Hermes retains
the trusted local-policy addition in its copied template.

No live account inference was run during implementation. Installation checks
cover the account controls and local enforcement; they do not establish server
acceptance or billing treatment. The source/mocks/live/billing distinctions above
remain in effect.
