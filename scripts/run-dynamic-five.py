"""Run only the requested five Cuppet tasks with live exponential enrichment."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CUPPET_BENCHMARK_SETTINGS", str(ROOT / "settings-dynamic-five.json"))
import benchmark
from harbor.job import Job
from harbor.models.job.config import JobConfig


def now():
    return datetime.now(timezone.utc).isoformat()


async def run(preflight=False):
    settings = benchmark.SETTINGS
    config = benchmark.configuration("cuppet", preflight=preflight)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{'preflight' if preflight else 'terminal-bench-4'}-cuppet-dynamic-five-{stamp}"
    config["job_name"] = name
    config["quiet"] = True
    if preflight:
        config["tasks"] = [{"path": str(ROOT / "preflight-dynamic")}]
        config["agents"][0]["kwargs"]["enrichment_interval_ms"] = 1000
    else:
        actual = [Path(task["path"]).name for task in config["tasks"]]
        if actual != settings["tasks"] or len(actual) != 5:
            raise RuntimeError(f"Unexpected task selection: {actual}")
    env = benchmark.docker_environment()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "DOCKER_DEFAULT_PLATFORM"):
        os.environ.pop(key, None)
    os.environ.update(env)
    folder = ROOT / "reports" / name
    folder.mkdir(parents=True, exist_ok=True)
    latest = ROOT / "reports" / ("dynamic-five-preflight.json" if preflight else "dynamic-five.json")
    state = {"pid": os.getpid(), "name": name, "state": "running", "started_at": now(),
             "settings": settings, "preflight": preflight, "job": f"jobs/{name}",
             "current_task": None, "phase": "starting", "completed": 0, "passed": 0, "failed": 0, "errors": 0, "trials": []}

    def persist():
        state["updated_at"] = now()
        text = json.dumps(state, indent=2) + "\n"
        for path in (folder / "status.json", latest):
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text)
            temporary.replace(path)

    benchmark.save(folder / "config.json", config)
    persist()
    try:
        job = await Job.create(JobConfig.model_validate(config))

        async def progress(event):
            state.update(current_task=event.task_name, phase=event.event.value)
            persist()

        async def ended(event):
            result = event.result
            error = result.exception_info.exception_type if result.exception_info else None
            reward = result.verifier_result.rewards.get("reward") if result.verifier_result else None
            trial = {"task": event.task_name, "trial": result.trial_name, "reward": reward, "error": error}
            summary_path = ROOT / state["job"] / result.trial_name / "agent" / "cuppet-result.json"
            if summary_path.exists():
                summary = json.loads(summary_path.read_text())
                trial.update(tst=summary["tst"], foreground_usage=(summary.get("sample") or {}).get("usage"),
                             dynamic_enrichment=summary.get("dynamicEnrichment"), source=summary.get("source"))
            state["trials"].append(trial)
            state["completed"] += 1
            state["errors" if error else "passed" if reward == 1 else "failed"] += 1
            state["phase"] = "cleanup"
            persist()
            print(f"{now()} {event.task_name}: reward={reward}; error={error}", flush=True)
            if not preflight:
                path = Path(next(task["path"] for task in config["tasks"] if Path(task["path"]).name == event.task_name.rsplit("/", 1)[-1]))
                task = tomllib.loads((path / "task.toml").read_text())
                references = []
                for image in (task.get("environment", {}).get("docker_image"), task.get("verifier", {}).get("environment", {}).get("docker_image")):
                    if image and image.startswith("harborframework/terminal-bench:"):
                        tag, _, digest = image.partition("@")
                        references.append(tag)
                        if digest:
                            references.append(f"harborframework/terminal-bench@{digest}")
                if references:
                    cleanup = await asyncio.to_thread(subprocess.run, ["docker", "image", "rm", *references], capture_output=True, text=True, timeout=120)
                    with (folder / "image-cleanup.log").open("a") as output:
                        output.write(cleanup.stdout + cleanup.stderr)

        for register in (job.on_trial_started, job.on_environment_started, job.on_agent_started, job.on_agent_ended, job.on_verification_started):
            register(progress)
        job.on_trial_ended(ended)
        await job.run()
        if state["completed"] != len(config["tasks"]):
            raise RuntimeError("The job ended without all selected task results")
        if preflight:
            trial = state["trials"][0]
            dynamic = trial.get("dynamic_enrichment") or {}
            events = dynamic.get("events", [])
            rounds = [event for event in events if event["type"] == "background.dynamic.completed"]
            retrieved = any(set(event.get("admittedKeys", [])) & set(event.get("retrievedKeys", [])) for event in rounds)
            delivered = any(event["type"] == "background.dynamic.delivered" for event in events)
            if trial["reward"] != 1 or trial["error"] or not retrieved or not delivered or not trial.get("tst", {}).get("connected"):
                raise RuntimeError("Dynamic preflight did not prove native TST admission, retrieval and foreground delivery")
            if trial.get("source", {}).get("cuppet_revision") != settings["cuppet_revision"] or trial.get("source", {}).get("system_prompt_sha256") != settings["system_prompt_sha256"]:
                raise RuntimeError("The preflight source or system prompt did not match the requested revision")
        state.update(state="complete", current_task=None, phase="complete", finished_at=now())
        persist()
        if not preflight:
            rows = ["# Cuppet dynamic enrichment: five-task run", "", f"GPT-6 Luna/max; source `{settings['cuppet_revision']}`.",
                    "", "Enrichment intervals: 1, 2, 4, 8… minutes after the first primary tool call, then after each completed enrichment attempt. The schedule resets per task.",
                    "Both foreground and background use Luna/max. Task prompts, final graders and original total solve budgets are unchanged.",
                    "", "| Task | Reward | Error | Enrichment rounds | Deliveries |", "| --- | ---: | --- | ---: | ---: |"]
            for trial in state["trials"]:
                events = (trial.get("dynamic_enrichment") or {}).get("events", [])
                rows.append(f"| {trial['task']} | {trial['reward']} | {trial['error'] or ''} | {sum(e['type']=='background.dynamic.completed' for e in events)} | {sum(e['type']=='background.dynamic.delivered' for e in events)} |")
            rows += ["", f"Passed: {state['passed']}; graded failures: {state['failed']}; errors: {state['errors']}.",
                     "", f"Detailed evidence: `reports/{name}/status.json` and `{state['job']}`.",
                     "", "The updated prompt and runtime differ from the previous October 17 baseline, so any difference cannot be attributed solely to memory enrichment."]
            (ROOT / "DYNAMIC-FIVE-RESULTS.md").write_text("\n".join(rows) + "\n")
        print(f"{now()} {name} complete: {state['passed']} passed, {state['failed']} failed, {state['errors']} errors.", flush=True)
    except BaseException as error:
        state.update(state="needs_attention", error=str(error), phase="stopped")
        persist()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    (ROOT / "reports").mkdir(exist_ok=True)
    with (ROOT / "reports" / "dynamic-five.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(preflight=args.preflight))
