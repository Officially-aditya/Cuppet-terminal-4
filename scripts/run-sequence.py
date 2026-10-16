"""Run the pinned 29-task Codex suite, then the identical Cuppet suite."""

import asyncio
import argparse
import copy
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import benchmark
from harbor.job import Job
from harbor.models.job.config import JobConfig

STATUS = ROOT / "reports" / "sequence.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def persist(state):
    state["updated_at"] = now()
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(STATUS)


async def release_task_images(task_path):
    """Release only this finished benchmark task's downloaded image references."""
    task = tomllib.loads((task_path / "task.toml").read_text())
    images = [task.get("environment", {}).get("docker_image"),
              task.get("verifier", {}).get("environment", {}).get("docker_image")]
    references = []
    for image in images:
        if image and image.startswith("harborframework/terminal-bench:"):
            tag, _, digest = image.partition("@")
            references.append(tag)
            if digest:
                references.append(f"harborframework/terminal-bench@{digest}")
    if references:
        result = await asyncio.to_thread(
            subprocess.run, ["docker", "image", "rm", *references],
            capture_output=True, text=True, timeout=120,
        )
        with (ROOT / "reports" / "image-cleanup.log").open("a") as output:
            output.write(f"{now()} {task_path.name}\n{result.stdout}{result.stderr}\n")


def resume_checkpoint(previous):
    state = copy.deepcopy(previous)
    old_settings = dict(state["settings"])
    new_settings = dict(benchmark.SETTINGS)
    old_settings.pop("agent_setup_timeout_sec", None)
    new_settings.pop("agent_setup_timeout_sec", None)
    if old_settings != new_settings:
        raise RuntimeError("Continuation changed benchmark settings beyond the approved setup timeout.")
    process = subprocess.run(["ps", "-p", str(int(state["pid"])), "-o", "comm="], capture_output=True, text=True)
    if process.returncode == 0:
        raise RuntimeError("The previous benchmark process is still alive; refusing to start a duplicate.")
    for current in state["agents"].values():
        prior_job = current["job"]
        current.setdefault("jobs", [prior_job] if (ROOT / prior_job).exists() else [])
        kept = []
        superseded = current.setdefault("superseded_trials", [])
        for trial in current["trials"]:
            trial.setdefault("job", prior_job)
            if trial["error"] in {"AgentSetupTimeoutError", "AgentAuthenticationError", "CancelledError"}:
                superseded.append(trial)
            else:
                kept.append(trial)
        names = [trial["task"].rsplit("/", 1)[-1] for trial in kept]
        if len(names) != len(set(names)):
            raise RuntimeError("Checkpoint contains duplicate completed tasks.")
        current.update(trials=kept, completed=len(kept),
                       passed=sum(trial["error"] is None and trial["reward"] == 1 for trial in kept),
                       failed=sum(trial["error"] is None and trial["reward"] != 1 for trial in kept),
                       errors=sum(trial["error"] is not None for trial in kept),
                       state="complete" if len(kept) == 29 else "queued")
    state.update(pid=os.getpid(), state="running", settings=benchmark.SETTINGS, resumed_at=now(),
                 current_agent=None, current_task=None, phase="resuming")
    state.pop("error", None)
    return state


async def run(resume=False):
    plans = {agent: benchmark.configuration(agent) for agent in ("codex", "cuppet")}
    assert len(plans["codex"]["tasks"]) == 29
    assert plans["codex"]["tasks"] == plans["cuppet"]["tasks"]
    paths = {Path(task["path"]).name: Path(task["path"]) for task in plans["codex"]["tasks"]}
    env = benchmark.docker_environment()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "DOCKER_DEFAULT_PLATFORM"):
        os.environ.pop(name, None)
    os.environ.update(env)
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "pid": os.getpid(), "state": "running", "started_at": now(),
        "settings": benchmark.SETTINGS, "order": ["codex", "cuppet"],
        "current_agent": None, "current_task": None, "phase": "starting",
        "agents": {agent: {"state": "queued", "job": f"jobs/{config['job_name']}",
                           "completed": 0, "passed": 0, "failed": 0, "errors": 0, "trials": []}
                   for agent, config in plans.items()},
    }
    if resume:
        previous = json.loads(STATUS.read_text())
        state = resume_checkpoint(previous)
        backup = ROOT / "reports" / "checkpoints" / f"sequence-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        benchmark.save(backup, previous)
    persist(state)
    try:
        for agent, config in plans.items():
            current = state["agents"][agent]
            completed_names = {trial["task"].rsplit("/", 1)[-1] for trial in current["trials"]}
            config["tasks"] = [task for task in config["tasks"] if Path(task["path"]).name not in completed_names]
            if not config["tasks"]:
                continue
            config["quiet"] = True
            benchmark.save(ROOT / "reports" / "configs" / f"{config['job_name']}.json", config)
            current.setdefault("jobs", [])
            current["job"] = f"jobs/{config['job_name']}"
            current["jobs"].append(current["job"])
            current["state"] = "running"
            state.update(current_agent=agent, current_task=None, phase="starting-job")
            persist(state)
            print(f"{now()} Starting {agent}: {config['job_name']}; {len(config['tasks'])} remaining tasks, Luna/max; {current['completed']} retained results.", flush=True)
            job = await Job.create(JobConfig.model_validate(config))

            async def progress(event):
                state.update(current_task=event.task_name, phase=event.event.value)
                persist(state)

            async def ended(event):
                result = event.result
                error = result.exception_info.exception_type if result.exception_info else None
                reward = result.verifier_result.rewards.get("reward") if result.verifier_result else None
                current["trials"].append({"task": event.task_name, "reward": reward, "error": error,
                                           "trial": result.trial_name, "job": current["job"]})
                current["completed"] += 1
                current["errors" if error else "passed" if reward == 1 else "failed"] += 1
                state["phase"] = "cleanup"
                persist(state)
                print(f"{now()} {agent} {current['completed']}/29: {event.task_name}; reward={reward}; error={error}", flush=True)
                task_name = event.task_name.rsplit("/", 1)[-1]
                try:
                    await release_task_images(paths[task_name])
                except Exception as cleanup_error:
                    print(f"Image cleanup: {cleanup_error}", flush=True)

            job.on_trial_started(progress)
            job.on_environment_started(progress)
            job.on_agent_started(progress)
            job.on_agent_ended(progress)
            job.on_verification_started(progress)
            job.on_trial_ended(ended)
            result = await job.run()
            completed_names = {trial["task"].rsplit("/", 1)[-1] for trial in current["trials"]}
            if current["completed"] != 29 or completed_names != set(paths) or result.finished_at is None:
                raise RuntimeError(f"{agent} did not finish all 29 trials; the next suite was not started.")
            current["state"] = "complete"
            current["finished_at"] = now()
            persist(state)
        state.update(state="complete", current_task=None, current_agent=None, phase="complete", finished_at=now())
        persist(state)
        benchmark.save(ROOT / "reports" / "comparison.json", state)
        lines = ["# Terminal-Bench 4: local 29-task comparison", "",
                 "GPT-6 Luna/max, Codex subscription, one attempt per task. Codex ran first, then Cuppet.", "",
                 "| Harness | Passed | Failed | Errors | Total |", "| --- | ---: | ---: | ---: | ---: |"]
        for agent, result in state["agents"].items():
            lines.append(f"| {agent} | {result['passed']} | {result['failed']} | {result['errors']} | 29 |")
        lines += ["", "Infrastructure errors are listed separately from graded failures.",
                  "Setup timeouts, authentication failures and interrupted trials are retained as superseded records when retried; completed graded results are preserved.",
                  "This is a CPU subset score, not a full Terminal-Bench leaderboard score.", "",
                  "Detailed local evidence: `reports/comparison.json` and the two job directories recorded there."]
        (ROOT / "RESULTS.md").write_text("\n".join(lines) + "\n")
        print(f"{now()} Both 29-task suites finished. See RESULTS.md.", flush=True)
    except BaseException as error:
        state.update(state="needs_attention", phase="stopped", error=str(error))
        persist(state)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="Continue a paused sequence, retaining completed results and retrying setup timeouts, authentication failures and interrupted work.")
    asyncio.run(run(resume=parser.parse_args().resume))
