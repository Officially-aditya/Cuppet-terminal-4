# Preflight verification

Verified on 2026-10-15 22:28 IST. **No Terminal-Bench tasks have been run.**

| Check | Result |
| --- | --- |
| Cuppet Desktop's native Mac runtime, Luna/max, native TST | PASS |
| Standard Codex through Harbor, Luna/max, subscription authentication | PASS |
| Cuppet Desktop runtime through Harbor, Luna/max, subscription authentication | PASS |
| Cuppet native TST graph connection inside the Linux container | PASS; connected |
| File read, exact uppercase file edit, and shell verification script | PASS on both harnesses |
| Harbor grading of the custom fixture | PASS on both harnesses |
| Paired 29-task configurations, Harbor `--dry-run` | PASS; no trials started |
| Exported agent logs contain no `auth.json` files | PASS |
| Real Terminal-Bench trials started | **0** |

The fixture is authored in this repository. Its successful grade is an integration
check, not a Terminal-Bench score.

## Pinned configuration

- Model: `gpt-6-luna`; reasoning: `max`.
- Official Codex CLI/app-server: `0.162.1`.
- Cuppet Desktop: `58270905de9e794f12c6dcf6b26e07a069e462ed`.
- Native TST source: `9f2a8ae63555b7534ee1ccfe211efce1ad46f630`.
- Native Linux TST binary SHA-256: `1fe944980bc3e47791e79f9e00d6802efd1bc226073abab6c235a6ca83b26b1b`.
- Preflight fixture SHA-256: `dbe040c6245584939ea77468ce67b8e102b19c19aef6cea99d04224df0e32d02`.
- Harbor: `0.24.0`; Linux platform: `linux/amd64`.
- One attempt and one concurrent trial for both configurations.
- Dataset definitions downloaded: `terminal-bench/terminal-bench@4.0.0`, 66 tasks.
- Local comparison profile: 29 tasks, no GPU, at most 2 CPUs / 4096 MB RAM.

## Local evidence

Generated evidence is retained locally and excluded from Git:

- `reports/preflight.json`: final machine-readable checks.
- `reports/host-cuppet.json`: native Mac runtime smoke check.
- `jobs/preflight-codex-20261015T165707Z`: final Codex fixture, trace and verifier result.
- `jobs/preflight-cuppet-20261015T165756Z`: final Cuppet fixture, trace, native state and verifier result.
- `reports/codex-plan.json` and `reports/cuppet-plan.json`: paired benchmark configurations.
- `.firecrawl/codex-plan-validation.log` and `.firecrawl/cuppet-plan-validation.log`: Harbor dry-run validation.

Live native state stays on the container filesystem, where its file locks work.
The runtime closes and flushes its state before that state is copied to the audit
logs. Subscription credentials stay outside those logs and are removed before
grading.

The Electron frontend and Windows packaging are outside this runtime preflight.
The full GPU/resource-heavy suite requires suitable infrastructure. The local
subset must be reported as a subset when actual benchmarking is started.
