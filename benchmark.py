"""Prepare Terminal-Bench, run a disposable preflight, or explicitly start a run."""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
SETTINGS = json.loads((ROOT / "settings.json").read_text())
DATASET = CACHE / "datasets" / "terminal-bench"
BUNDLE = CACHE / "cuppet-runtime.tar.gz"
HARBOR = Path(sys.executable).parent / ("harbor.exe" if os.name == "nt" else "harbor")


def execute(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, cwd=ROOT, **kwargs)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def docker_environment():
    env = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "DOCKER_DEFAULT_PLATFORM"):
        env.pop(key, None)
    if sys.platform == "darwin":
        endpoint = execute(
            ["docker", "context", "inspect", SETTINGS["docker_context"], "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        env["DOCKER_HOST"] = endpoint
    config_dir = CACHE / "docker"
    config_dir.mkdir(parents=True, exist_ok=True)
    plugin_dirs = [str(p) for p in (
        Path("/opt/homebrew/lib/docker/cli-plugins"),
        Path("/usr/local/lib/docker/cli-plugins"),
    ) if p.exists()]
    save(config_dir / "config.json", {"cliPluginsExtraDirs": plugin_dirs})
    env["DOCKER_CONFIG"] = str(config_dir)
    env["PYTHONPATH"] = str(ROOT)
    return env


def prepare(cuppet_source):
    CACHE.mkdir(exist_ok=True)
    if cuppet_source:
        source = Path(cuppet_source).expanduser().resolve()
    else:
        source = CACHE / "cuppet-git"
        if not (source / ".git").exists():
            execute(["git", "clone", "--filter=blob:none", "--no-checkout", SETTINGS["cuppet_repository"], source])
    archive = execute(
        ["git", "-C", source, "archive", SETTINGS["cuppet_revision"], "src", "package.json"],
        capture_output=True,
    ).stdout
    snapshot = CACHE / "cuppet-source"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(snapshot, filter="data")

    json5 = source / "node_modules" / "json5"
    if not json5.is_dir():
        dependencies = CACHE / "dependencies"
        save(dependencies / "package.json", {"private": True, "dependencies": {"json5": "2.2.3"}})
        execute(["npm", "install", "--prefix", dependencies, "--ignore-scripts", "--no-audit", "--no-fund"])
        json5 = dependencies / "node_modules" / "json5"
    if json.loads((json5 / "package.json").read_text())["version"] != "2.2.3":
        raise RuntimeError("Cuppet requires json5 2.2.3 for this pinned revision.")
    shutil.copytree(json5, snapshot / "node_modules" / "json5")
    shutil.copy2(ROOT / "scripts" / "cuppet-run.mjs", snapshot / "cuppet-run.mjs")

    # Compile exactly the native TST revision shipped with this desktop build.
    # musl produces a portable binary for the benchmark's different Linux bases.
    tst_archive = CACHE / "tst-source.tar.gz"
    if not tst_archive.exists():
        url = f"https://codeload.github.com/Officially-aditya/Cuppet-code/tar.gz/{SETTINGS['tst_revision']}"
        urllib.request.urlretrieve(url, tst_archive)
    tst_build = CACHE / "tst-source"
    if not tst_build.exists():
        with tarfile.open(tst_archive) as archive_file:
            archive_file.extractall(CACHE / "tst-unpacked", filter="data")
        extracted = CACHE / "tst-unpacked" / f"Cuppet-code-{SETTINGS['tst_revision']}"
        shutil.move(str(extracted), tst_build)
    native = CACHE / "tst-linux" / "tst-daemon"
    env = docker_environment()
    execute(["docker", "info", "--format", "{{.ServerVersion}}"], env=env)
    if not native.is_file():
        execute([
            "docker", "buildx", "build", "--platform", SETTINGS["platform"],
            "--file", ROOT / "scripts" / "Dockerfile.tst",
            "--output", f"type=local,dest={native.parent}", tst_build,
        ], env=env)
    native.chmod(0o755)
    runtime = snapshot / "vendor" / "tst" / "linux-x64-gnu"
    runtime.mkdir(parents=True)
    shutil.copy2(native, runtime / "tst-daemon")
    identity = {
        "cuppet_repository": SETTINGS["cuppet_repository"],
        "cuppet_revision": SETTINGS["cuppet_revision"],
        "tst_revision": SETTINGS["tst_revision"],
        "tst_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
        "codex_version": SETTINGS["codex_version"],
        "json5_version": "2.2.3",
    }
    save(snapshot / "benchmark-source.json", identity)
    with tarfile.open(BUNDLE, "w:gz") as archive_file:
        for entry in sorted(snapshot.iterdir()):
            archive_file.add(entry, arcname=entry.name)
    save(CACHE / "bundle.json", {**identity, "bundle_sha256": hashlib.sha256(BUNDLE.read_bytes()).hexdigest()})

    if len(list(DATASET.glob("*/task.toml"))) != 66:
        execute([HARBOR, "datasets", "download", SETTINGS["dataset"], "--output-dir", CACHE / "datasets"])
    create_task_manifest()
    print("Prepared Cuppet, native Linux TST, and 66 task definitions. No benchmark tasks ran.")


def create_task_manifest():
    included, excluded = [], []
    paths = sorted(DATASET.glob("*/task.toml"))
    if len(paths) != 66:
        raise RuntimeError(f"Expected 66 Terminal-Bench 4.0 task definitions, found {len(paths)}.")
    for path in paths:
        environment = tomllib.loads(path.read_text()).get("environment", {})
        reasons = []
        if environment.get("gpus", 0):
            reasons.append("requires GPU")
        if environment.get("cpus", 1) > 2:
            reasons.append("requires more than 2 CPUs")
        if environment.get("memory_mb", 1024) > 4096:
            reasons.append("requires more than 4096 MB RAM")
        record = {
            "name": path.parent.name,
            "task_config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if reasons:
            excluded.append({**record, "reasons": reasons})
        else:
            included.append(record)
    save(ROOT / "configs" / "local-cpu-tasks.json", {
        "dataset": SETTINGS["dataset"], "profile": "local CPU subset",
        "max_cpus": 2, "max_memory_mb": 4096, "tasks": included, "excluded": excluded,
    })


def configuration(agent, preflight=False):
    if not BUNDLE.is_file():
        raise RuntimeError("Run prepare first; the pinned Cuppet runtime bundle is missing.")
    auth_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    if not auth_path.is_file():
        raise RuntimeError("Run codex login using ChatGPT; a file-based auth.json is required.")
    common = {"model_name": SETTINGS["model"], "override_setup_timeout_sec": SETTINGS["agent_setup_timeout_sec"],
              "env": {"CODEX_AUTH_JSON_PATH": str(auth_path)}}
    if agent == "codex":
        agent_config = {**common, "name": "codex", "kwargs": {
            "version": SETTINGS["codex_version"], "reasoning_effort": SETTINGS["reasoning_effort"],
            "web_search": "disabled",
        }}
    else:
        agent_config = {**common, "import_path": "cuppet_agent:CuppetDesktopAgent", "kwargs": {
            "bundle_path": str(BUNDLE), "codex_version": SETTINGS["codex_version"],
            "reasoning_effort": SETTINGS["reasoning_effort"], "timeout_sec": 300 if preflight else 0,
        }}
    if preflight:
        task_paths = [ROOT / "preflight"]
    else:
        manifest = json.loads((ROOT / "configs" / "local-cpu-tasks.json").read_text())
        task_paths = []
        for task in manifest["tasks"]:
            path = DATASET / task["name"]
            if hashlib.sha256((path / "task.toml").read_bytes()).hexdigest() != task["task_config_sha256"]:
                raise RuntimeError(f"Task definition changed: {task['name']}")
            task_paths.append(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "job_name": f"{'preflight' if preflight else 'terminal-bench-4'}-{agent}-{stamp}",
        "jobs_dir": str(ROOT / "jobs"),
        "n_attempts": SETTINGS["attempts"], "n_concurrent_trials": SETTINGS["concurrency"],
        "retry": {"max_retries": 0},
        "agents": [agent_config],
        "environment": {"type": "docker", "delete": True, "extra_docker_compose": [str(ROOT / "configs" / "platform.yaml")]},
        "tasks": [{"path": str(path)} for path in task_paths],
    }


def run_job(config, env):
    from harbor.models.job.config import JobConfig
    JobConfig.model_validate(config)
    path = ROOT / "reports" / "configs" / f"{config['job_name']}.json"
    save(path, config)
    log = ROOT / "reports" / f"{config['job_name']}.log"
    with log.open("w") as output:
        execute([HARBOR, "run", "--config", path], env=env, stdout=output, stderr=subprocess.STDOUT)
    return ROOT / "jobs" / config["job_name"]


def preflight(selected_agent="both"):
    env = docker_environment()
    execute(["docker", "info", "--format", "{{.ServerVersion}}"], env=env)
    execute(["docker", "compose", "version"], env=env)
    fixture_hash = hashlib.sha256()
    for path in sorted((ROOT / "preflight").rglob("*")):
        if path.is_file():
            fixture_hash.update(str(path.relative_to(ROOT)).encode())
            fixture_hash.update(path.read_bytes())
    report = {"benchmark_started": False, "fixture": "preflight", "fixture_sha256": fixture_hash.hexdigest(), "settings": SETTINGS, "model": SETTINGS["model"],
              "reasoning_effort": SETTINGS["reasoning_effort"], "checks": {}}
    report_path = ROOT / "reports" / "preflight.json"
    if selected_agent != "both" and report_path.exists():
        previous = json.loads(report_path.read_text())
        if previous.get("settings") == SETTINGS and previous.get("fixture_sha256") == report["fixture_sha256"]:
            report["checks"] = previous["checks"]
    agents = ("codex", "cuppet") if selected_agent == "both" else (selected_agent,)
    for agent in agents:
        print(f"Running {agent} preflight (one disposable fixture).", flush=True)
        try:
            config = configuration(agent, preflight=True)
            # This is the only execution path used by preflight.
            assert [task["path"] for task in config["tasks"]] == [str(ROOT / "preflight")]
            job = run_job(config, env)
            trials = list(job.glob("*/result.json"))
            if len(trials) != 1:
                raise RuntimeError(f"Expected one fixture trial, found {len(trials)}; inspect {job}.")
            trial = json.loads(trials[0].read_text())
            rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
            passed = trial.get("exception_info") is None and rewards.get("reward") == 1.0
            check = {"passed": passed, "job": str(job.relative_to(ROOT)), "rewards": rewards}
            if agent == "cuppet":
                results = list(job.glob("*/agent/cuppet-result.json"))
                if len(results) != 1:
                    raise RuntimeError("Cuppet did not export its runtime evidence.")
                summary = json.loads(results[0].read_text())
                events_path = results[0].parent / "cuppet-events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines()]
                tools = sorted({event["tool"] for event in events if event.get("type") == "tool.finished" and event.get("success")})
                check.update({"tst_mode": summary["tst"]["mode"], "tst_connected": summary["tst"]["connected"], "tools": tools})
                command_ran = bool({"cuppet_execute", "bash", "tst_validate"}.intersection(tools))
                check["passed"] &= summary["tst"]["mode"] == "managed-native" and summary["tst"]["connected"] and command_ran and "PREFLIGHT_OK" in summary["text"]
            report["checks"][agent] = check
        except Exception as error:
            report["checks"][agent] = {"passed": False, "error": str(error)}
        save(report_path, report)
        print(json.dumps({agent: report["checks"][agent]}), flush=True)
    report["passed"] = set(report["checks"]) == {"codex", "cuppet"} and all(check["passed"] for check in report["checks"].values())
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(report_path, report)
    if not report["passed"]:
        raise RuntimeError(f"Preflight needs attention; inspect {report_path} and reports/*.log.")
    print("Both preflights passed. No Terminal-Bench tasks ran.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Download and build prerequisites; do not run tasks.")
    prep.add_argument("--cuppet-source", help="Optional local Git checkout; only the pinned committed revision is copied.")
    smoke = commands.add_parser("preflight", help="Test both real harnesses on the disposable fixture only.")
    smoke.add_argument("--agent", choices=("both", "codex", "cuppet"), default="both", help="Optionally retry one harness, preserving matching results for the other.")
    commands.add_parser("plan", help="Validate and write both benchmark configurations without running them.")
    run = commands.add_parser("run", help="Explicitly start real Terminal-Bench tasks (never called by prepare/preflight).")
    run.add_argument("--agent", choices=("codex", "cuppet"), required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.cuppet_source)
    elif args.command == "preflight":
        preflight(args.agent)
    elif args.command == "plan":
        from harbor.models.job.config import JobConfig
        for agent in ("codex", "cuppet"):
            config = configuration(agent)
            JobConfig.model_validate(config)
            path = ROOT / "reports" / f"{agent}-plan.json"
            save(path, config)
            print(f"{agent}: {len(config['tasks'])} tasks; configuration saved to {path}. Nothing started.")
    else:
        job = run_job(configuration(args.agent), docker_environment())
        print(f"Results: {job}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
