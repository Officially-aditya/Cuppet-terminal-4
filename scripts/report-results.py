"""Build the final report from saved trials; no model or Docker calls are made."""

import json
import statistics
import collections
import hashlib
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
STATE = json.loads((ROOT / "reports/sequence.json").read_text())
METRICS = ["solve_seconds", "setup_seconds", "verification_seconds", "trial_wall_seconds",
           "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
           "reasoning_tokens", "total_tokens"]


def seconds(start, end):
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    assert value >= 0
    return value


def phase(result, name):
    value = result.get(name)
    return seconds(value["started_at"], value["finished_at"]) if value else 0


def load(path):
    path = path.resolve()
    assert path.is_relative_to(ROOT / "jobs")
    return json.loads((path / "result.json").read_text())


def usage(path, result, agent):
    raw = result.get("agent_result") or {}
    values = [raw.get(k) for k in ("n_input_tokens", "n_cache_tokens", "n_output_tokens")]
    reasoning = None
    source = "Harbor agent_result"
    if agent == "codex" and (path / "agent/trajectory.json").exists():
        trajectory = json.loads((path / "agent/trajectory.json").read_text())
        final = trajectory.get("final_metrics") or {}
        reasoning = (final.get("extra") or {}).get("reasoning_output_tokens")
        if all(v is not None for v in values):
            assert values == [final[k] for k in ("total_prompt_tokens", "total_cached_tokens", "total_completion_tokens")]
    if agent == "cuppet" and (path / "agent/cuppet-result.json").exists():
        summary = json.loads((path / "agent/cuppet-result.json").read_text())
        final = (summary.get("sample") or {}).get("usage") or {}
        reasoning = final.get("reasoningTokens")
        if all(v is not None for v in values):
            assert values == [final[k] for k in ("inputTokens", "cachedInputTokens", "outputTokens")]
            assert final["totalTokens"] == values[0] + values[2]
    if not all(v is not None for v in values):
        if not result.get("agent_execution"):
            values, reasoning, source = [0, 0, 0], 0, "Agent never started"
        elif agent == "cuppet" and (path / "agent/cuppet-events.jsonl").exists():
            events = []
            for line in (path / "agent/cuppet-events.jsonl").read_text().splitlines():
                event = json.loads(line)
                if (event.get("type") == "runtime.activity" and event.get("source") == "provider"
                        and event.get("activity", {}).get("type") == "activity.usage"
                        and isinstance(event["activity"].get("usage"), dict)):
                    events.append(event)
            assert events and len({event.get("messageId") for event in events}) == 1
            last = events[-1]["activity"]["usage"]
            for key in ("inputTokens", "cachedInputTokens", "outputTokens"):
                assert all(a["activity"]["usage"][key] <= b["activity"]["usage"][key] for a, b in zip(events, events[1:]))
            values = [last[k] for k in ("inputTokens", "cachedInputTokens", "outputTokens")]
            reasoning = last.get("reasoningTokens")
            source = "Last cumulative provider activity.usage before interruption; recorded lower bound"
        else:
            values = [None, None, None]
            source = "Usage unavailable for this failed/interrupted agent execution"
    inputs, cache, outputs = values
    if inputs is not None:
        assert 0 <= cache <= inputs and 0 <= (reasoning or 0) <= outputs
    return {"input_tokens": inputs, "cached_input_tokens": cache,
            "uncached_input_tokens": inputs - cache if inputs is not None else None,
            "output_tokens": outputs, "reasoning_tokens": reasoning,
            "total_tokens": inputs + outputs if inputs is not None else None,
            "usage_source": source}


def trial_path(trial, agent_state):
    return ROOT / trial.get("job", agent_state["job"]) / trial["trial"]


def retained(trial, agent, agent_state):
    path = trial_path(trial, agent_state)
    chain = []
    while True:
        result = load(path)
        assert result["finished_at"]
        chain.append((path.resolve(), result))
        source = (result.get("config") or {}).get("source_trial")
        if not source and (path / "config.json").exists():
            source = json.loads((path / "config.json").read_text()).get("source_trial")
        if not source:
            break
        assert source["action"] == "regrade" and len(chain) < 5
        path = Path(source["path"])
        assert path.resolve() not in {p for p, _ in chain}
    final_path, final = chain[0]
    solve_path, solve = chain[-1]
    assert final["exception_info"] is None and solve["agent_execution"]
    assert final["agent_result"] == solve["agent_result"]
    assert final["verifier_result"]["rewards"]["reward"] == trial["reward"]
    measurement = usage(solve_path, solve, agent)
    assert all(measurement[k] is not None for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
    return {"trial": trial["trial"], "reward": trial["reward"],
            "final_trial_path": str(final_path), "solve_trial_path": str(solve_path),
            "source_paths": [str(p) for p, _ in chain], "task_checksum": solve["task_checksum"],
            "solve_seconds": phase(solve, "agent_execution"),
            "setup_seconds": sum(phase(d, "environment_setup") + phase(d, "agent_setup") for _, d in chain),
            "verification_seconds": sum(phase(d, "verifier") for _, d in chain),
            "trial_wall_seconds": sum(seconds(d["started_at"], d["finished_at"]) for _, d in chain),
            **measurement}


def aggregate(rows):
    total = {k: sum(row[k] for row in rows if row.get(k) is not None) for k in METRICS}
    total.update(n=len(rows), median_solve_seconds=statistics.median(row["solve_seconds"] for row in rows),
                 missing_token_records=sum(row.get("total_tokens") is None for row in rows))
    total["cache_fraction"] = total["cached_input_tokens"] / total["input_tokens"] if total["input_tokens"] else None
    return total


indexed = {a: {t["task"]: t for t in d["trials"]} for a, d in STATE["agents"].items()}
common = sorted(set(indexed["codex"]) & set(indexed["cuppet"]))
matched = [task for task in common if all(indexed[a][task]["error"] is None for a in indexed)]
excluded = [{"task": task, "reason": {a: indexed[a][task]["error"] for a in indexed}}
            for task in common if task not in matched]
rows = []
sources = {a: set() for a in indexed}
for task in matched:
    row = {"task": task.rsplit("/", 1)[-1]}
    for agent in indexed:
        row[agent] = retained(indexed[agent][task], agent, STATE["agents"][agent])
        sources[agent].update(row[agent]["source_paths"])
    assert row["codex"]["task_checksum"] == row["cuppet"]["task_checksum"]
    row["time_ratio"] = row["cuppet"]["solve_seconds"] / row["codex"]["solve_seconds"]
    row["token_ratio"] = row["cuppet"]["total_tokens"] / row["codex"]["total_tokens"]
    rows.append(row)

summary = {agent: aggregate([r[agent] for r in rows]) for agent in indexed}
without_photonic = {agent: aggregate([r[agent] for r in rows if r["task"] != "photonic-waveguide-routing"]) for agent in indexed}
extra = {agent: [] for agent in indexed}
for agent, state in STATE["agents"].items():
    for trial in state.get("superseded_trials", []):
        path = trial_path(trial, state).resolve()
        if trial["task"] not in matched or str(path) in sources[agent]:
            continue
        result = load(path)
        extra[agent].append({"task": trial["task"].rsplit("/", 1)[-1], "trial_path": str(path),
                             "error": trial["error"], "reason": trial.get("superseded_reason"),
                             "solve_seconds": phase(result, "agent_execution"),
                             "setup_seconds": phase(result, "environment_setup") + phase(result, "agent_setup"),
                             "verification_seconds": phase(result, "verifier"),
                             "trial_wall_seconds": seconds(result["started_at"], result["finished_at"]),
                             **usage(path, result, agent)})
extra_totals = {a: aggregate(records) for a, records in extra.items()}
all_attempts = {a: aggregate([r[a] for r in rows] + extra[a]) for a in indexed}
paired = {"n": len(rows), "cuppet_faster": sum(r["time_ratio"] < 1 for r in rows),
          "cuppet_fewer_total_tokens": sum(r["token_ratio"] < 1 for r in rows),
          "median_time_ratio": statistics.median(r["time_ratio"] for r in rows),
          "median_token_ratio": statistics.median(r["token_ratio"] for r in rows),
          "codex_passes": sum(r["codex"]["reward"] == 1 for r in rows),
          "cuppet_passes": sum(r["cuppet"]["reward"] == 1 for r in rows)}
report = {"snapshot_at_utc": NOW.isoformat(), "sequence_updated_at": STATE["updated_at"],
          "model": STATE["settings"]["model"], "reasoning_effort": STATE["settings"]["reasoning_effort"],
          "completed_counts": {a: d["completed"] for a, d in STATE["agents"].items()},
          "excluded_common_tasks": excluded, "paired": paired, "retained_totals": summary,
          "without_photonic": without_photonic, "extra_attempt_totals": extra_totals,
          "all_attempts_recorded_totals": all_attempts, "extra_attempts": extra, "tasks": rows}
def duration(value):
    minutes, secs = divmod(round(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


assert STATE["state"] == "complete"
assert all(a["completed"] == 29 and len(a["trials"]) == 29 for a in STATE["agents"].values())
all_results = {agent: [] for agent in indexed}
tool_stats = {}
for agent, arm in STATE["agents"].items():
    counts = collections.Counter()
    execution = collections.Counter()
    traces = 0
    connected = 0
    for trial in sorted(arm["trials"], key=lambda t: t["task"]):
        if trial["error"] is None:
            record = retained(trial, agent, arm)
        else:
            path = trial_path(trial, arm).resolve()
            result = load(path)
            assert result["exception_info"]["exception_type"] == trial["error"]
            record = {"trial": trial["trial"], "reward": trial["reward"],
                      "final_trial_path": str(path), "solve_trial_path": str(path),
                      "source_paths": [str(path)], "task_checksum": result["task_checksum"],
                      "solve_seconds": phase(result, "agent_execution"),
                      "setup_seconds": phase(result, "environment_setup") + phase(result, "agent_setup"),
                      "verification_seconds": phase(result, "verifier"),
                      "trial_wall_seconds": seconds(result["started_at"], result["finished_at"]),
                      **usage(path, result, agent)}
        record.update(task=trial["task"].rsplit("/", 1)[-1], error=trial["error"])
        all_results[agent].append(record)
        source = Path(record["solve_trial_path"])
        result = load(source)
        context = result.get("agent_result") or {}
        metadata = context.get("metadata") or {}
        if agent == "cuppet":
            native = metadata.get("execution")
            if native:
                traces += 1
                connected += bool(metadata.get("tst_connected"))
                counts.update(native.get("toolCallsByName") or {})
                for key in ["total", "executed", "completed", "successes", "failures", "durationMs", "outputBytes", "mutations", "validationAttempts", "validationSuccesses", "validationFailures", "blockedRawReads", "blockedRawMutations"]:
                    execution[key] += native.get(key, 0)
        elif (source / "agent/trajectory.json").exists():
            traces += 1
            trajectory = json.loads((source / "agent/trajectory.json").read_text())
            for step in trajectory["steps"]:
                execution["trajectory_steps"] += 1
                for call in step.get("tool_calls") or []:
                    counts[call["function_name"]] += 1
        record["result_sha256"] = hashlib.sha256((Path(record["final_trial_path"]) / "result.json").read_bytes()).hexdigest()
    tool_stats[agent] = {"traces": traces, "tst_connected_traces": connected if agent == "cuppet" else None,
                         "calls_by_name": dict(counts.most_common()), "counters": dict(execution)}

all_retained = {a: aggregate(records) for a, records in all_results.items()}
all_spent = {a: aggregate(records + extra[a]) for a, records in all_results.items()}
timeline = {}
for agent, records in all_results.items():
    paths = {p for record in records for p in record["source_paths"]} | {record["trial_path"] for record in extra[agent]}
    originals = [load(Path(p)) for p in paths]
    first = min(datetime.fromisoformat(d["started_at"]) for d in originals)
    last = max(datetime.fromisoformat(d["finished_at"]) for d in originals)
    elapsed = (last - first).total_seconds()
    timeline[agent] = {"started_at": first.isoformat(), "finished_at": last.isoformat(),
                       "elapsed_seconds": elapsed, "unique_trial_records": len(paths),
                       "outside_recorded_trial_seconds": elapsed - all_spent[agent]["trial_wall_seconds"]}

preflights = []
for path in sorted((ROOT / "jobs").glob("preflight-*/*/result.json")):
    result = json.loads(path.read_text())
    agent = "cuppet" if path.parents[1].name.startswith("preflight-cuppet-") else "codex"
    try:
        measured = usage(path.parent, result, agent)
    except (AssertionError, KeyError):
        measured = {k: None for k in METRICS if k.endswith("tokens")}
        measured["usage_source"] = "No completed usage record for this diagnostic attempt"
    preflights.append({"path": str(path.parent), "agent": agent,
                       "reward": (result.get("verifier_result") or {}).get("rewards", {}).get("reward"),
                       "error": (result.get("exception_info") or {}).get("exception_type"),
                       "solve_seconds": phase(result, "agent_execution"),
                       "setup_seconds": phase(result, "environment_setup") + phase(result, "agent_setup"),
                       "verification_seconds": phase(result, "verifier"),
                       "trial_wall_seconds": seconds(result["started_at"], result["finished_at"]), **measured})
auxiliary = {a: aggregate([r for r in preflights if r["agent"] == a]) for a in indexed}
host_probes = []
for path in sorted((ROOT / "reports").glob("host-cuppet*/cuppet-result.json")):
    raw = json.loads(path.read_text())
    host_probes.append({"path": str(path), "status": raw.get("status"),
                        "usage": (raw.get("sample") or {}).get("usage"),
                        "elapsed_ms": (raw.get("sample") or {}).get("elapsedMs")})
resources = {}
for name in sorted(t.rsplit("/", 1)[-1] for t in indexed["codex"]):
    definition = tomllib.loads((ROOT / "reports/task-definitions" / f"{name}.toml").read_text())
    resources[name] = {"environment": {k: v for k, v in definition.get("environment", {}).items() if k in ["cpus", "memory_mb", "storage_mb", "gpus"]},
                       "agent_timeout_sec": definition.get("agent", {}).get("timeout_sec"),
                       "verifier_timeout_sec": definition.get("verifier", {}).get("timeout_sec")}
report.update(scope="complete", settings=STATE["settings"], sequence_started_at=STATE["started_at"],
              sequence_finished_at=STATE["finished_at"],
              sequence_elapsed_seconds=seconds(STATE["started_at"], STATE["finished_at"]),
              all_retained_totals=all_retained, all_attempts_all_tasks=all_spent,
              all_task_results=all_results, tools=tool_stats, timeline=timeline,
              preflight_trials=preflights, preflight_totals=auxiliary, host_probes=host_probes,
              task_resources=resources, runtime_bundle=json.loads((ROOT / "reports/runtime-bundle.json").read_text()),
              host=json.loads((ROOT / "reports/runtime-platform.json").read_text()))
(ROOT / "reports/final-metrics.json").write_text(json.dumps(report, indent=2) + "\n")


def millions(value):
    return f"{value / 1e6:.3f}M"


def pct_change(codex, cuppet):
    return f"{(cuppet / codex - 1) * 100:+.1f}%"


def table(header, records):
    return ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |",
            *["| " + " | ".join(str(cell) for cell in record) + " |" for record in records], ""]


def compared(records, keys):
    return table(["Metric", "Codex", "Cuppet", "Cuppet change"],
                 [(label, form(records["codex"][key]), form(records["cuppet"][key]),
                   pct_change(records["codex"][key], records["cuppet"][key])) for label, key, form in keys])


def grade(record):
    return "Error" if record["error"] else "Pass" if record["reward"] == 1 else "Fail"


time_keys = [("Agent solve time", "solve_seconds", duration), ("Environment + agent installation", "setup_seconds", duration),
             ("Verification", "verification_seconds", duration), ("Recorded trial wall time", "trial_wall_seconds", duration)]
token_keys = [("Input, including cached", "input_tokens", millions), ("Cached input (subset)", "cached_input_tokens", millions),
              ("Uncached input", "uncached_input_tokens", millions), ("Output, including reasoning", "output_tokens", millions),
              ("Reasoning output (subset)", "reasoning_tokens", millions), ("Total input + output", "total_tokens", millions)]
final_rows = {r["task"]: {} for r in all_results["codex"]}
for agent, records in all_results.items():
    for record in records:
        final_rows[record["task"]][agent] = record
assert len(final_rows) == 29
settings = STATE["settings"]
elapsed = report["sequence_elapsed_seconds"]
out = ["# Cuppet Desktop vs Codex: final Terminal-Bench 4 report", "",
       "GPT-6 Luna, max reasoning, Codex subscription, local 29-task CPU subset. Completed 17 October 2026 at 08:37 IST.", "",
       "## Findings", "",
       "Cuppet finished with **4 passes, 25 graded failures and no infrastructure errors**. Codex finished with **3 passes, 24 graded failures and 2 infrastructure errors**. This is one additional pass across the selected 29 tasks, a difference of 3.45 percentage points. These are retained final results after documented recovery, rather than clean first-attempt scores.", "",
       "On the 27 tasks graded by both harnesses, Cuppet used **22.8% less agent solve time and 24.3% fewer total tokens**. The large photonic solve changes the aggregate: without it, Cuppet took **20.9% more solve time and 12.2% more total tokens**. Cuppet was faster on 12/27 paired tasks and used fewer total tokens on 9/27. The median paired ratios were 1.21× time and 1.53× tokens.", "",
       "Across all selected tasks and recorded recovery attempts, measured trial wall time was almost identical: about **16h40m Codex and 16h42m Cuppet**. Cuppet used fewer raw tokens including cache, but more uncached input and output. The data supports a task-dependent difference in behavior; it does not establish a consistent speed, token, or quality improvement.", "",
       "## Final outcomes", ""]
out += table(["Harness", "Pass", "Graded fail", "Infrastructure error", "Selected tasks", "Pass / selected"],
             [(a.title(), d["passed"], d["failed"], d["errors"], 29, f"{d['passed']/29:.2%}") for a,d in STATE["agents"].items()])
out += ["Both harnesses passed photonic-waveguide-routing. Cuppet additionally passed coq-block-bound, heat-pump-warranty, and mvcc-lsm-compaction. Codex additionally passed kv-live-surgery and react-lead-form.", "",
        "The two Codex infrastructure failures were foodstuff-beta-activity (`NonZeroAgentExitCodeError`, installation failed before model execution) and vllm-deepseek-streaming (`EnvironmentStartTimeoutError`, environment startup exceeded its budget). They remain errors, rather than graded model failures. Cuppet reached grading on both and received zero reward.", "",
        "## Experiment and scope", ""]
out += table(["Setting", "Recorded configuration"], [
    ("Dataset", settings["dataset"]), ("Scope", "29 of 66 versioned tasks; no GPU, at most 2 CPUs and 4096 MB RAM"),
    ("Model / reasoning", "gpt-6-luna / max"), ("Provider / authentication", "Official Codex subscription authentication"),
    ("Codex runtime / Harbor", "0.162.1 / 0.24.0"), ("Benchmark Python", "3.12.4"),
    ("Cuppet revision", settings["cuppet_revision"]), ("Native TST revision", settings["tst_revision"]),
    ("Host", "Apple Silicon arm64, 8 GiB RAM"), ("Dedicated VM", "Colima benchmark profile; 2 CPUs, 4 GiB RAM, 25 GiB virtual disk; native aarch64"),
    ("Task platform", "Linux amd64, VZ/Rosetta compatibility"), ("Ordering / concurrency", "All Codex tasks first, then all Cuppet tasks; one task at a time"),
    ("Per-job attempts", "1; manual infrastructure recoveries and interruptions listed separately"),
    ("Task solve budget", "Original task definitions, 28,800 seconds (8 hours) per agent attempt"),
    ("Installation budget", "Originally 360 seconds; increased to 1,800 seconds for both harnesses"),
    ("Built-in web search", "Disabled"), ("Cuppet policy", "Optimized execution, native managed TST, normal context/plans/memory/kernel; background enrichment paused")])
out += ["Cuppet ran the unmodified runtime process used by its Electron desktop app inside the Linux task container. Its desktop window and Windows packaging were outside this evaluation. Cuppet's provider used the pinned Codex app-server; the baseline used Harbor's standard Codex loop with a small credential-ownership adapter.", "",
        "Each task used a fresh container, agent session, and Cuppet data directory. Task instructions, checksums, model selection, reasoning effort, and solve budgets were preserved. The host, Docker image cache, VM, subscription account, and fixed harness ordering were shared.", "",
        "This is a local resource-filtered subset report, not an official full Terminal-Bench leaderboard score. No GPU tasks or cloud sandbox were used.", "",
        "## Measurement definitions", "",
        "- **Agent solve time:** Harbor's `agent_execution` interval, including model responses, tools, native graph work, and waits. It is not isolated model-compute time.",
        "- **Setup:** environment startup plus agent installation. **Verification:** the recorded official verifier interval. **Trial wall time:** the complete trial duration, including artifact handling and gaps between phases.",
        "- **Retained results:** one final result for each selected task, following a regrade back to its original solve. A regrade adds verifier/wall time but does not add model time or usage again.",
        "- **All recorded attempts:** retained results plus superseded records, with regraded source trials deduplicated. This includes the two-hour interrupted Cuppet wait and earlier interrupted Codex model runs.",
        "- **Token counts:** cumulative provider-reported input plus output. Cached input is already included in input. Reasoning output is already included in output. Neither subset is added a second time.",
        "- **Matched comparison:** only the 27 tasks with a completed grade on both harnesses. The two Codex infrastructure failures are excluded from paired efficiency statistics, while remaining present in all-29 operational totals.", "",
        "Codex counts were checked against its saved ATIF final metrics. Cuppet counts were checked against its saved runtime sample. Interrupted Cuppet Risk-scorer usage was recovered from the last cumulative provider event, which is a recorded lower bound. A failed Codex authentication execution had no reported token count. These counts do not describe the subscription's quota formula or actual dollar billing.", "",
        "## Timing", "", "**Matched 27 retained results**", ""]
out += compared(summary, time_keys)
out += ["**All 29 selected tasks: retained trials, including infrastructure failures**", ""]
out += compared(all_retained, time_keys)
out += ["**All 29 selected tasks: including superseded attempts**", ""]
out += compared(all_spent, time_keys)
out += ["Measured trial wall time is the sum of recorded trial durations, rather than elapsed time between the first and last task. The difference captures pauses, image cleanup, and orchestration outside trial records.", ""]
out += table(["Timeline", "Codex", "Cuppet"], [
    ("First recorded trial (IST)", *[datetime.fromisoformat(timeline[a]["started_at"]).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d Oct %H:%M:%S") for a in indexed]),
    ("Last recorded trial (IST)", *[datetime.fromisoformat(timeline[a]["finished_at"]).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d Oct %H:%M:%S") for a in indexed]),
    ("Elapsed span", *[duration(timeline[a]["elapsed_seconds"]) for a in indexed]),
    ("Outside recorded trials", *[duration(timeline[a]["outside_recorded_trial_seconds"]) for a in indexed]),
    ("Unique trial records, including regrades", *[timeline[a]["unique_trial_records"] for a in indexed])])
out += [f"The complete coordinator sequence lasted **{duration(elapsed)}**, from 15 October 22:38:57 IST to 17 October 08:37:18 IST. Preflight and preparation were outside this sequence.", "",
        "## Tokens", "", "**Matched 27 retained results**", ""]
out += compared(summary, token_keys)
out += table(["Matched-task cache / output composition", "Codex", "Cuppet"], [
    ("Cached share of input", f"{summary['codex']['cache_fraction']:.2%}", f"{summary['cuppet']['cache_fraction']:.2%}"),
    ("Reasoning share of output", *[f"{summary[a]['reasoning_tokens']/summary[a]['output_tokens']:.2%}" for a in indexed]),
    ("Uncached input + output", *[millions(summary[a]['uncached_input_tokens']+summary[a]['output_tokens']) for a in indexed])])
out += ["**All 29 selected tasks, including superseded attempts: recorded token totals**", ""]
out += compared(all_spent, token_keys)
out += ["The lower raw total should not be read as lower fresh-token generation: Cuppet's matched uncached input was higher, and so was its output. Much of both harnesses' raw usage was reused input context. No comparable monetary-cost figure is available: Harbor's Codex `cost_usd` is an estimate, Cuppet's field is null, and neither is a subscription charge ledger.", "",
        "## Per-task results, solve times, and tokens", "",
        "Times and usage below are for the retained solve. Error rows did not reach model execution. Tokens are input plus output, including cached input. `Error` is separate from a graded `Fail`.", ""]
records=[]
for name,pair in final_rows.items():
    a,b=pair['codex'],pair['cuppet']
    records.append((name,f"{grade(a)} / {grade(b)}",duration(a['solve_seconds']) if not a['error'] else '—',duration(b['solve_seconds']),
                    duration(a['trial_wall_seconds']),duration(b['trial_wall_seconds']),
                    millions(a['total_tokens']) if not a['error'] else '—',millions(b['total_tokens'])))
out += table(["Task", "Grade Codex / Cuppet", "Codex solve", "Cuppet solve", "Codex trial", "Cuppet trial", "Codex tokens", "Cuppet tokens"], records)
out += ["## Task variation and the photonic effect", "",
        "![Per-task time and token ratios](assets/task-ratios.svg)", "",
        "Ratios below 1 favor Cuppet for that resource; ratios above 1 mean Cuppet used more. These are resource comparisons and do not imply a pass on the task.", "",
        "Photonic passed on both harnesses: Codex took 5h17m33s and 87.893M tokens; Cuppet took 1h30m18s and 10.179M tokens. Its saved Cuppet output was regraded after a verifier download failure, without a new model solve. This one task changes the direction of the aggregate comparison.", ""]
out += compared(without_photonic, [time_keys[0], token_keys[-1]])
out += ["Across the 27 pairs, the median Cuppet/Codex task ratio was **1.207× time** and **1.525× total tokens**. Cuppet was faster on 12 tasks and slower on 15; it used fewer raw tokens on 9 and more on 18. Cuppet's additional passes involved more solve time on Coq, Heat-pump, and MVCC. Conversely, its shorter KV solve failed where Codex passed.", "",
        "## Tool activity and native runtime", "",
        "These counters describe retained runs. The tools operate at different granularities, so call totals are not interchangeable measures of agent decisions or efficiency.", ""]
out += table(["Measurement", "Codex", "Cuppet"], [
    ("Retained model traces", tool_stats['codex']['traces'], tool_stats['cuppet']['traces']),
    ("Recorded tool calls / attempts", sum(tool_stats['codex']['calls_by_name'].values()), tool_stats['cuppet']['counters']['total']),
    ("Native TST connected", "Not applicable", f"{tool_stats['cuppet']['tst_connected_traces']}/29"),
    ("Executed / completed operations", "Not normalized", f"{execution['executed']} / {execution['completed']}"),
    ("Operation success / failure", "Not normalized", f"{execution['successes']} / {execution['failures']}"),
    ("Cumulative tool duration", "Not collected comparably", duration(execution['durationMs']/1000)),
    ("Recorded tool output bytes", "Not collected comparably", f"{execution['outputBytes']:,}"),
    ("Recorded mutations", "Not normalized", execution['mutations']),
    ("Local validation success / failure", "Not normalized", f"{execution['validationSuccesses']} / {execution['validationFailures']}")])
out += table(["Cuppet tool", "Recorded attempts"], tool_stats['cuppet']['calls_by_name'].items())
out += table(["Codex tool", "Recorded calls"], tool_stats['codex']['calls_by_name'].items())
out += ["Cuppet completed 5,538 executed operations with 5,404 recorded successes (97.58%). Tool success means the operation completed under the runtime's contract; it does not imply the benchmark task passed. Risk-scorer alone accounts for 3,747 of the 5,604 recorded Cuppet calls, about 66.9%, and still received zero reward. Codex's `exec` calls can each contain multiple operations, while Cuppet's execution kernel records finer-grained activity. Cumulative tool durations can overlap or include nested work; they are not an exclusive CPU-time partition.", "",
        "## Recovery and retry accounting", "",
        "Seven Codex records and eight Cuppet records are superseded in the checkpoint. One on each side is the original solve behind a regrade, rather than an extra model solve. There are 36 unique Codex trial records and 37 unique Cuppet trial records, excluding integration preflights.", ""]
out += table(["Extra superseded work, matched tasks", "Codex", "Cuppet"], [
    ("Extra agent time", *[duration(extra_totals[a]['solve_seconds']) for a in indexed]),
    ("Extra setup + verification", *[duration(extra_totals[a]['setup_seconds']+extra_totals[a]['verification_seconds']) for a in indexed]),
    ("Extra recorded tokens", *[millions(extra_totals[a]['total_tokens']) for a in indexed])])
out += ["- The installation budget was raised from 6 to 30 minutes after slow setup caused timeouts. Task-solving budgets were preserved.",
        "- Codex batched-eval-parity completed a model solve before a verifier architecture problem. Its saved output was regraded after fixing the architecture setting; the final grade was zero. Its original model usage and solve time are counted once.",
        "- Subscription authentication failed on a non-root task because copied credentials were owned by UID 501 and unreadable by the task's `nobody` user. The account credentials had not expired. Both adapters were corrected to assign the copied private credentials to the actual container UID; both non-root preflights passed afterward.",
        "- Local network failures affected Cuppet image downloads. Heat-pump and KV were retried; their original failed setups never reached the model. Production-planning, React-lead-form, and Retro-console were also retried after failures before model execution. Photonic's existing output was regraded, rather than solved again.",
        "- Codex Coq, KV, and Roy have earlier interrupted model executions. Cuppet Risk-scorer was explicitly interrupted after its output stopped and restarted in a fresh session. Its first execution spent 2h22m05s, with 4.091M tokens recorded before interruption. The retained retry failed grading.",
        "- The later request to kill a task was clarified by the user as a status request. Retro-console was left running; no additional retry was made then.", "",
        "Fresh containers and sessions prevented reuse of an interrupted solution's local state. Manual restarts still change the sampling and total-compute history, so these results should not be labeled clean pass@1. There is no evidence here of cross-harness state reuse or hidden-test feedback being supplied to a retry, but this run does not establish freedom from prior model training exposure to task material.", "",
        "## Preflights and checks outside the score", "",
        "The latest non-root Codex and Cuppet fixtures both passed before continuation. These checked real Luna/max read/edit/shell behavior and Cuppet's native graph connection. They are not benchmark tasks and are excluded from the score and main resource tables.", ""]
out += table(["Logged integration fixtures", "Codex", "Cuppet"], [
    ("Recorded fixture trials", auxiliary['codex']['n'], auxiliary['cuppet']['n']),
    ("Recorded agent time", *[duration(auxiliary[a]['solve_seconds']) for a in indexed]),
    ("Recorded input + output", *[millions(auxiliary[a]['total_tokens']) for a in indexed])])
out += table(["Additional host-only Cuppet fixture", "Status", "Recorded runtime", "Input + output"],
             [(Path(p['path']).parent.name,p['status'],duration(p['elapsed_ms']/1000),millions(p['usage']['totalTokens'])) for p in host_probes])
out += ["These host fixtures consumed another 239,581 recorded tokens in total and are excluded from the benchmark comparison. This report does not include the tokens used by this conversation, unlogged diagnostic requests, or other activity on the shared subscription.", "",
        "## Limits and unmeasured quantities", "",
        "This was one local CPU subset run with fixed ordering, infrastructure repairs, and manual interruptions. There were no repeated independent runs or randomized harness ordering. The task count is small, most tasks failed, and one pass separates the harnesses. No broad superiority or official leaderboard ranking follows from this result.", "",
        "Peak RAM, CPU utilization over time, energy, provider service latency, subscription quota debits, and actual monetary charges were not recorded comprehensively. VM allocations are configuration limits, not measurements of consumption. The end-to-end agent times include tools, code execution, compatibility overhead, and idle waits, so they do not isolate model generation speed.", "",
        "## Evidence and reproduction", "",
        "- [Final result summary](RESULTS.md), [run settings](settings.json), [selected-task manifest](configs/local-cpu-tasks.json), [setup and integration details](README.md).",
        "- Local machine-readable measurements: `reports/final-metrics.json`; final checkpoint and trial references: `reports/comparison.json`; credential/regrade/retry audit records are retained locally.",
        "- Raw job logs, trajectories, artifacts, and supporting metadata are preserved in the verified local archive `reports/benchmark-evidence-20261017.tar.gz`. The archive and raw logs are excluded from Git. `reports/benchmark-evidence-20261017.tar.gz.sha256` and `reports/evidence-manifest.json` provide its hash and file verification results.",
        "- Recover raw records and host fixtures, then regenerate statistics without Docker or model calls:", "", "```sh", "tar -xzf reports/benchmark-evidence-20261017.tar.gz jobs reports/host-cuppet reports/host-cuppet-retry", "python3 scripts/report-results.py", "```", "",
        "Re-running model evaluations requires rebuilding the dedicated environment with `./scripts/setup-mac.sh`, authenticating through the existing Codex subscription, and explicitly starting the requested tasks. Regenerating this report does not re-run an agent.", "",
        f"Pinned native TST SHA-256: `{report['runtime_bundle']['tst_sha256']}`. Runtime bundle SHA-256: `{report['runtime_bundle']['bundle_sha256']}`.", "",
        "Cleanup status and storage accounting are recorded in `reports/cleanup.json`.", ""]
cleanup_path = ROOT / "reports/cleanup.json"
if cleanup_path.exists():
    cleanup = json.loads(cleanup_path.read_text())
    if cleanup.get("state") == "complete":
        out += ["## Cleanup completed", "",
                "The dedicated Colima benchmark VM and its container data were deleted, including the remaining 13 images and 35 build-cache records. No containers or volumes remained before teardown. The benchmark Docker context was removed, the CLI was returned to `default`, and the existing Docker Desktop context was preserved. The completed benchmark monitor was removed.", "",
                "The dataset/build/source caches, isolated `.venv`, temporary research files, unpacked job directories, and host/native test workspaces were removed. Shared command-line tools, the existing Codex login, and the user's Cuppet checkout were retained.", "",
                f"All {cleanup['verified_archive_entries']:,} archive entries were verified against their originals, including SHA-256 for every regular file. The archive preserves all 81 logged benchmark/preflight trial records. Its size is {cleanup['archive_mib']:.1f} MiB. The removed paths formerly accounted for approximately {cleanup['former_allocated_footprint_gib']:.2f} GiB of allocated files/VM storage; this is a `du` footprint, not a measured net free-space increase and may include shared/hardlinked blocks.", "",
                f"Archive SHA-256: `{cleanup['archive_sha256']}`.", ""]
(ROOT / "REPORT.md").write_text("\n".join(out))
print(json.dumps({"report":str(ROOT/'REPORT.md'), "metrics":str(ROOT/'reports/final-metrics.json'),
                  "matched_tasks":len(rows),"completed_tasks_per_harness":29},indent=2))
