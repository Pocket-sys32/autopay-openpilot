# Generic QR-driven parking agent — session journal

This document preserves the working context from the implementation session that produced the
`parking-agent` branch. It is intended as the durable handoff for future work, review, deployment,
and incident analysis.

## Goal

Extend the existing parking flow from two hard-coded destinations into a web-first agent that can:

1. Accept a previously unseen HTTPS parking QR.
2. Drive the provider's mobile website in Chrome on the Android VM.
3. Use Gemini through Vertex AI to choose one bounded UI action at a time.
4. Read the checkout total deterministically from the page.
5. Stop before payment and show the exact quote on the comma device.
6. Require a deliberate slide-to-confirm gesture on the device HUD.
7. Re-read all price and session invariants immediately before the pay click.
8. Pay only if the live checkout still exactly matches what the driver authorized.

The existing Google Form demo and LAZ adapters remain deterministic fast paths.

## Decisions made

- Web-first: app-only flows return `APP_REQUIRED`; Play Store automation is not attempted.
- Confirmation happens on the comma device HUD.
- Real payment is supported, but is bounded independently by the device and backend maximum totals.
- Gemini runs through Vertex AI using the VM service account and metadata-server credentials; no API
  key is introduced.
- The LLM chooses the next UI action, while deterministic code owns the browser session, profile,
  secrets, state transitions, price parsing, safety caps, and payment boundary.
- CAPTCHA, account creation, MFA, and OTP are never bypassed. They become `action_required` outcomes.
- A browser session is held open while the driver decides. With one emulator this deliberately blocks
  the worker claim loop for at most the confirmation timeout.
- Losing a held session invalidates the quote. A restart never reconstructs a checkout and silently
  honors an old confirmation.

## Load-bearing safety invariants

The LLM's claimed price is not trusted. `price.parse_total_minor()` supplies the value that is:

- checked against the configured cap;
- stored in the checkout summary;
- hashed into the confirmation token;
- rendered on the device;
- and compared for exact equality immediately before payment.

The `quote_hash` binds the user's decision to the exact summary shown on the HUD. A stale, altered, or
unseen quote cannot be confirmed.

The model cannot address arbitrary selectors or coordinates. A DOM harvest exposes a bounded set of
visible interactive nodes with ephemeral `data-pa-nid` identifiers. Identifiers are removed and
reissued on every observation. Cross-origin frames and payment fields receive no node id.

Card values never enter prompts, model responses, screenshots, or model-visible DOM. The model names a
secret slot; deterministic code supplies the value only during the post-confirmation phase.

`mark_submitting()` executes immediately before the pay click. Therefore interruption before that mark
is known unpaid; interruption after it is `unknown` and is never retried automatically.

## Milestones completed

### M0 — prerequisite repair and reconciliation

- Removed the invalid `flaresolverr==1.3.0` Python dependency.
- Replaced the nonexistent client import with the FlareSolverr HTTP API.
- Made the LAZ adapter import lazy so generic/demo workers do not depend on LAZ startup requirements.
- Fixed the public result flag that mislabeled generic real-money attempts as demos.
- Repaired positional `Settings` construction in tests and made new fields append-only with defaults.
- Reconciled the `flaresolverr-http` worktree's attach-to-running-Chrome, browser warm-up, reCAPTCHA
  detection, diagnostics, and deployment scripts.
- Kept Cloudflare cookie injection opt-in; it is incompatible with attach mode.
- Moved shared provider exceptions to `parking_backend/errors.py`.

### M1 — confirmation state machine and decision API

- Added `confirmation_required` and `committing` backend states.
- Added an in-place SQLite migration for quote and decision columns.
- Added `await_confirmation()` and transactional, replay-safe `record_decision()`.
- Added `POST /v1/attempts/{attempt_id}/decision` with 400/404/409/410 behavior.
- Bounded the public confirmation response to a small, redacted structure.
- Extended crash recovery so stale confirmation sessions expire, pre-submit commits fail safely, and
  submitted attempts remain unknown rather than being retried.

### M2 — two-phase worker and held browser sessions

- Added `CheckoutSummary` and the `ConfirmingAdapter` protocol.
- Kept existing adapters on their unchanged one-shot path.
- Added a live held-session loop with decision polling, keepalive, timeout, teardown, and SIGTERM
  cleanup.
- Added separate prepare, confirm, and commit timeouts.
- Added offline lifecycle coverage for confirm, decline, timeout, restart, post-submit interruption,
  and legacy adapter behavior.

### M3 — device protocol and HUD confirmation

- Unknown well-formed HTTPS QR URLs now become `generic_agent` candidates.
- Added offline URL policy checks: no credentials, non-default ports, IP literals, local names,
  punycode, shorteners, oversized payloads, or non-HTTPS schemes.
- Added generic schema v2 with `qr_url` and a device-controlled `max_total_minor`.
- Replaced exact payload-set checks with strict per-provider specifications while retaining v1
  compatibility and rejection of unknown fields.
- Added `post_decision()`, confirmation/committing phases, `AWAITING_CONFIRMATION`, and the related
  Params.
- Added roll-away cancellation so a car leaving during confirmation never gets charged.
- Added a slide-to-confirm dialog. Dismissing it records a refusal rather than leaving a live checkout
  stranded.
- Added confirmation previews and UI/status coverage.

### M4 — bounded agent core against fakes

- Added the strict action schema, profile boundary, secret vault, prompt builder, price parser,
  validator, observe/decide/act loop, and `GenericAgentAdapter`.
- Added phase gating, domain constraints, step budgets, navigation-loop detection, stale-node handling,
  typed-text hygiene, quote freezing, and pre-pay re-verification.
- Added a fully offline fake browser and scripted model.
- Exercised the real adapter through the real worker and store with only browser/model faked.
- Added an explicit `DRY_RUN` outcome that stops before the click.

### M5 — real browser and Vertex client

- Added the live DOM harvester and redacted screenshot capture.
- Added the Appium `DriverSession` with attach mode, a 600-second command timeout, and the native value
  setter needed by framework-controlled inputs.
- Added `VertexGeminiClient` using the VM metadata server, REST `generateContent`, temperature zero,
  and a per-call timeout.
- Registered the generic adapter in `worker.main()` behind `PARKING_AGENT_ENABLED`.
- Added separate generic-agent card configuration.
- Expanded `parking_backend/README.md` with emulator sign-in, deployment settings, IAM, dry-run, and
  browser probing instructions.

## Important bugs found during implementation

- Raising `DecisionExpired` inside the transaction rolled back the expiry write. The exception is now
  raised only after the transaction commits.
- API tests originally mixed synthetic fixture time with the real endpoint clock, making live calls
  appear expired.
- Punycode hosts initially passed the ASCII hostname check. `xn--` labels are now rejected.
- Treating short secret strings as raw substrings made CVV `123` collide with `DEMO123` and
  `123 Main St`. Only sufficiently distinctive long literals are matched globally; payment inputs are
  protected structurally and labelled CVVs are redacted contextually.
- Same-screen loop detection originally fired during commit, where filling multiple fields on one page
  is normal. It now applies only while navigating; commit remains bounded by its own step budget.
- A decision response was initially routed through PUT bookkeeping, which would have cleared pending
  state. Decision responses now only update the stored remote body.
- The production scanner changes that predated this session could not be discarded: committed
  `parkingd` constructs `VisionQRScanner(backend_provider=...)`, and the uncommitted scanner work was
  the implementation of that API. Those changes were reviewed and committed instead.

## Commits pushed

Branch: `parking-agent`, tracking `autopay/parking-agent`.

- `948a2e7a7` — parking: stop at checkout and require a confirmed decision before paying
- `4d2bc7a38` — parking: accept an unknown sign and ask the driver before paying
- `496bc7c65` — parking: decode QR on the VM and gate test mode to a stopped car
- `22be189f2` — parking: give the agent a real browser and a real model
- `af407f0d8` — parking: document deploying the generic agent

The upstream `origin` points to commaai/openpilot. Feature work was pushed only to the user's `autopay`
fork.

## Verification at the M5 handoff

- 169 backend tests passed in the openpilot environment, with seven API tests skipped there because
  FastAPI is not installed.
- The seven API tests passed in a separate scratch environment with FastAPI.
- 68 device/UI tests passed.
- Ruff/lint checks passed for the touched backend, device, and UI trees.
- The production scanner constructed successfully.
- The real worker wiring imported and constructed successfully with injected configuration.
- No real merchant checkout, Vertex request, payment, or VM deployment had been attempted.

## Deployment prerequisites before a live dry run

Grant the VM service account Vertex access:

```bash
gcloud projects add-iam-policy-binding fieldscout-497018 \
  --member serviceAccount:parking-demo-vm@fieldscout-497018.iam.gserviceaccount.com \
  --role roles/aiplatform.user
```

On the VM, disable LAZ Cloudflare cookie injection so attach mode remains active:

```text
PARKING_LAZ_INJECT_CF_COOKIES=0
```

Enable the generic adapter in dry-run mode first:

```text
PARKING_AGENT_ENABLED=1
PARKING_AGENT_DRY_RUN=1
```

Chrome must already be open and signed in on the emulator. Run `deploy/probe_browser.py` before a live
checkout; it is read-only and never clicks PAY.

## Remaining work at handoff

### M6 — live validation and payment

1. Deploy the branch to the VM.
2. Verify service health and Vertex authorization.
3. Probe attach-mode Chrome, Cloudflare, and reCAPTCHA state.
4. Run a real unknown-provider navigation with `PARKING_AGENT_DRY_RUN=1`.
5. Inspect the harvested DOM, redacted screenshots, latency, quote, and HUD confirmation.
6. Only after dry-run evidence is clean, run one explicitly authorized paid session through the generic
   adapter rather than the LAZ fast path.

### M7 — hardening and observability

Completed after the M5 handoff:

- Added mode-0600, metadata-only JSON diagnostics with a 50-file retention bound.
- Recorded action and model latency, screenshot usage, and Vertex prompt/candidate/total token counts.
- Stopped resending an unchanged navigation screenshot while continuing to send every commit screenshot.
- Added precise public outcomes for model outages, price caps, off-domain navigation, stuck/step-budget
  failures, invariant drift, and generic CAPTCHA blocking.
- Made deterministic CAPTCHA detection stop the loop before the model is asked for an action.
- Moved `PriceLimitExceeded` into the shared errors module so the worker remains independent of the LAZ
  adapter import path.
- Fixed generic paid attempts to report `AGENT_PAID` rather than the demo-form success reason.
- Added tests for telemetry, deduplicated screenshots, diagnostics permissions/redaction/retention, and
  model-outage behavior.

Still useful as follow-up hardening: add captured provider/page fixtures for common failure modes and a
larger adversarial prompt-injection corpus after the first dry-run DOMs are available.

## Current continuation point

Commit and push the completed M7 hardening before touching the VM. M6 requires real infrastructure and an
explicitly controlled dry run; payment must remain disabled until dry-run evidence has been reviewed.
