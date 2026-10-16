# Cuppet Desktop vs Codex: Terminal-Bench 4

This prepares a paired comparison of **GPT-6 Luna with max reasoning** in Cuppet
Desktop and the standard Codex harness, using an existing **ChatGPT/Codex
subscription**. API keys and Modal are not required.

`prepare` builds/downloads prerequisites. `preflight` runs only the small fixture
in this repository. `plan` writes configurations without executing tasks. Only
the explicit `run` command starts Terminal-Bench tasks.

## Configuration

| Setting | Both harnesses |
| --- | --- |
| Model | `gpt-6-luna` |
| Reasoning | `max` |
| Authentication | ChatGPT sign-in via the official Codex runtime |
| Codex version | `0.162.1` |
| Dataset | `terminal-bench/terminal-bench@4.0.0` |
| Platform | Linux x86-64 in local Docker |
| Attempts / concurrency | 1 / 1 |
| Agent installation time budget | 30 minutes |
| Task timeouts and resources | Original task definitions |
| Built-in web search | Disabled |

The Cuppet source revision and matching native TST source revision are pinned in
`settings.json`. Both runs start with fresh task containers and fresh agent state.
Cuppet uses its normal optimized execution policy, context compiler, project
graph, plans, memory, execution kernel and tool runtime. Background enrichment
is paused for this foreground comparison. Each task gets a new chat and data
directory. The same Codex version supplies Cuppet's model connection and the
baseline's agent loop.

## Set up on this Mac

The required tools have been installed into Homebrew. Harbor uses this folder's
isolated `.venv`. Docker runs in the dedicated `benchmark` Colima profile with
2 CPUs, 4 GB RAM and a 25 GB data disk. Rosetta supplies x86-64 compatibility on
Apple Silicon. Start the VM after a reboot with:

```sh
colima start --profile benchmark
```

The task service is pinned to x86-64 in `configs/platform.yaml`. Docker's global
default platform is cleared so Harbor's verifier helpers can use the VM's native
architecture.

For a fresh installation, install Homebrew and sign in with `codex login` first.
Rosetta must be installed on Apple Silicon. Then:

```sh
./scripts/setup-mac.sh
```

An existing local Cuppet source checkout can avoid cloning:

```sh
./bench prepare --cuppet-source /path/to/Cuppet-desktop
```

Only the pinned, committed source is copied. Uncommitted changes in that checkout
are excluded. `prepare` builds the native TST daemon from its pinned source as a
static Linux x86-64 binary and stages it with Cuppet. The original checkout and
the desktop application's saved chats/settings are untouched.

Authentication must be available as `~/.codex/auth.json`, or under `CODEX_HOME`.
The preflight uses that existing login. Credentials are copied into a private
temporary directory inside each disposable container, kept outside exported
agent logs, and removed before verification. Credentials, caches and generated
logs are excluded from Git.

Credential copies keep private permissions and are owned by the actual container
user. The small Codex adapter resolves that user before calling Harbor's standard
Codex run method; Cuppet applies the same ownership rule. The preflight runs as
`nobody` to check subscription authentication without root access.

## Run the preflight

```sh
./bench preflight
```

This runs one short, real Luna/max tool-using task per harness on a custom
fixture: read a text file, create an uppercase copy, and run its verification
script through the shell. Harbor checks the exact file contents and the script's
completion marker. Cuppet's check also requires
the native TST daemon, a live native graph connection, and a recorded successful
command execution. These calls consume
subscription usage. They are integration checks, not Terminal-Bench scores.

The result is saved to `reports/preflight.json`. Full traces and runtime evidence
are under `jobs/preflight-*`, including Cuppet's `cuppet-events.jsonl`,
`cuppet-result.json` and isolated conversation database.
Live Cuppet state uses the container filesystem so native TST file locks work;
the state is copied into the audit directory after the runtime shuts down.

## How the desktop app is evaluated

The adapter launches **`src/runtime/main.mjs`, the exact runtime process used by
Cuppet's Electron desktop app**, and communicates through the same JSON protocol
as the desktop frontend. It selects the Codex subscription provider and
Luna/max for that task. It does not implement another agent loop or redirect
operations to the host Mac.

The runtime and native TST daemon execute inside Harbor's Linux task container.
Cuppet uses its Full Access session mode there, matching the standard Codex
baseline's unrestricted access within the same disposable container. The runner
refuses Full Access outside Docker. The Electron window is not part of this
terminal benchmark; the app's harness is. This preflight validates the runtime
integration, not GUI interaction or Windows packaging.

## Prepared benchmark scope

All 66 versioned task definitions are downloaded. The checked-in
`configs/local-cpu-tasks.json` selects tasks requesting no GPU, at most 2 CPUs,
and at most 4096 MB RAM (29 tasks); every excluded task has a recorded reason. Both harnesses
use exactly that same task list and preserve each task's original limits.

This 8 GB Mac cannot run the complete GPU/resource-heavy benchmark. The local
subset is suitable for a paired comparison; its score must be reported as a
subset score. A full official leaderboard comparison requires appropriate
Linux/GPU infrastructure and the full task set.

Inspect the paired configurations without starting them:

```sh
./bench plan
```

When ready to start the actual benchmark, run these explicitly, sequentially:

```sh
./bench run --agent codex
./bench run --agent cuppet
```

Task results and verifier rewards are written to separate directories under
`jobs/terminal-bench-4-*`. Keep both runs on the same pinned source, model,
reasoning level, task manifest and VM configuration. Do not mix preflight
rewards into benchmark results.

The sequential coordinator in `scripts/run-sequence.py` runs all 29 Codex tasks
before starting the same 29 Cuppet tasks. Live progress is written atomically to
`reports/sequence.json`. The current background launch also writes to
`reports/sequence-launch.log` and uses `caffeinate` to prevent idle sleep.
Finished tasks' downloaded benchmark images are released after grading to keep
the 25 GB Docker disk available; task logs and results are retained. When both
suites finish, the coordinator writes `reports/comparison.json` and `RESULTS.md`,
with infrastructure errors listed separately from graded failures.

After a graceful interruption, `scripts/run-sequence.py --resume` continues from
the saved checkpoint. It retains completed results, retries setup timeouts,
authentication failures and interrupted trials in a new continuation job, and keeps the original attempts
as superseded audit records. Job paths are recorded per harness in the progress
file. Increasing the installation budget does not change task-solving timeouts.

To stop the VM and reclaim its RAM when finished:

```sh
colima stop --profile benchmark
```

## Sources

- [Official OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth)
- [Official OpenAI model documentation](https://learn.chatgpt.com/docs/models)
- [Harbor's Codex integration](https://github.com/harbor-framework/harbor/blob/main/src/harbor/agents/installed/codex.py)
- [Terminal-Bench 4.0 release](https://github.com/harbor-framework/terminal-bench/releases/tag/v4.0.0)
- [Cuppet's native TST artifact contract](https://github.com/Officially-aditya/Cuppet-code/blob/9f2a8ae63555b7534ee1ccfe211efce1ad46f630/docs/tst-runtime-artifacts.md)
