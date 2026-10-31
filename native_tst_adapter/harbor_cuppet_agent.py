from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Literal

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import ConfigDict


class CuppetOptions(AgentOptions):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["original", "continue", "fresh", "structured"]
    task_id: str
    state_dir: str
    cuppet_root: str
    node_bin: str = "node"


class CuppetAgent(BaseAgent):
    options_model = CuppetOptions

    @staticmethod
    def name() -> str:
        return "cuppet-agentstress"

    def version(self) -> str | None:
        return "pilot-5 (gpt-6-luna, max; original cap 25m)"

    async def setup(self, environment: BaseEnvironment) -> None:
        del environment

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        condition = self.options.condition
        task_id = self.options.task_id
        state_dir = Path(self.options.state_dir)
        task_state = state_dir / task_id
        attempt_dir = task_state / condition
        workspace = attempt_dir / "workspace"
        data_dir = attempt_dir / "data"
        node_runner = Path(__file__).with_name("cuppet_trial.mjs")
        workdir = str(environment.task_env_config.workdir or "/app")
        unlimited = os.environ.get("AGENTSTRESS_UNLIMITED") == "1"
        timeout_override = int(os.environ.get("AGENTSTRESS_TIMEOUT_SEC", "0"))
        timeout_sec = None if unlimited else (timeout_override or (1500 if condition == "original" else 240))

        baseline: dict | None = None
        baseline_result_path = task_state / "original" / "agentstress.json"
        if condition == "original":
            shutil.rmtree(attempt_dir, ignore_errors=True)
            workspace.mkdir(parents=True, exist_ok=True)
            await environment.download_dir(workdir, workspace)
        else:
            if not baseline_result_path.is_file():
                result = {"condition": condition, "status": "skipped", "reason": "original state is missing"}
                self._save_result(attempt_dir, result)
                context.metadata = {"agentstress": result}
                return
            baseline = json.loads(baseline_result_path.read_text())
            if not baseline.get("natural_completion"):
                result = {"condition": condition, "status": "skipped", "reason": "original attempt did not finish naturally"}
                self._save_result(attempt_dir, result)
                context.metadata = {"agentstress": result}
                return
            shutil.rmtree(attempt_dir, ignore_errors=True)
            shutil.copytree(task_state / "original" / "workspace", workspace)
            if condition == "continue":
                shutil.copytree(task_state / "original" / "data", data_dir)
            else:
                data_dir.mkdir(parents=True, exist_ok=True)
            await environment.empty_dirs([workdir])
            await environment.upload_dir(workspace, workdir)

        if task_id == "risk-scorer-replay":
            try:
                await self._restore_task_write_access(environment, workdir)
            except Exception as exc:
                result = {
                    "condition": condition,
                    "task_id": task_id,
                    "status": "controller_error",
                    "natural_completion": False,
                    "model_id": "gpt-6-luna",
                    "reasoning_effort": "max",
                    "error": f"task write-access preflight failed: {exc}"[:3000],
                }
                self._save_result(attempt_dir, result)
                context.metadata = {"agentstress": result}
                return

        data_dir.mkdir(parents=True, exist_ok=True)
        lock = asyncio.Lock()
        server = await asyncio.start_server(
            lambda reader, writer: self._handle_bridge(
                reader,
                writer,
                environment=environment,
                workspace=workspace,
                workdir=workdir,
                lock=lock,
            ),
            host="127.0.0.1",
            port=0,
        )
        port = server.sockets[0].getsockname()[1]
        payload = {
            "condition": condition,
            "task_id": task_id,
            "instruction": instruction,
            "workspace": str(workspace),
            "data_dir": str(data_dir),
            "baseline": ({
                "session_id": baseline.get("session_id"),
                "project_id": baseline.get("project_id"),
            } if baseline else None),
            "handoff_path": str(task_state / "original" / "structured_handoff.json"),
            "timeout_sec": timeout_sec,
            "model_id": "gpt-6-luna",
            "reasoning_effort": "max",
        }
        child_env = os.environ.copy()
        child_env.update(
            {
                "CUPPET_ROOT": self.options.cuppet_root,
                "AGENTSTRESS_BRIDGE_URL": f"http://127.0.0.1:{port}",
                "CUPPET_NONINTERACTIVE": "1",
                "CUPPET_PE3": "0",
                "CUPPET_ORCHESTRATOR": "0",
                "AGENTSTRESS_HEADLESS_TOOL_POLICY": "1",
            }
        )
        child_env.pop("CUPPET_TST_SOCKET", None)
        child_env.pop("CUPPET_TST_TOKEN", None)

        started_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                self.options.node_bin,
                str(node_runner),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
            try:
                communicate = process.communicate(json.dumps(payload).encode())
                if timeout_sec is None:
                    stdout, stderr = await communicate
                else:
                    stdout, stderr = await asyncio.wait_for(communicate, timeout=timeout_sec + 150)
            except TimeoutError:
                process.kill()
                stdout, stderr = await process.communicate()
                raise RuntimeError("Cuppet controller exceeded its outer timeout")
            if process.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace")[-3000:] or f"node exited {process.returncode}")
            lines = stdout.decode(errors="replace").splitlines()
            marker = next((line for line in reversed(lines) if line.startswith("AGENTSTRESS_RESULT=")), None)
            if marker is None:
                raise RuntimeError("Cuppet controller returned no result record")
            result = json.loads(marker.partition("=")[2])
            result["bridge_elapsed_ms"] = round((time.monotonic() - started_at) * 1000)
            result["provider_wall_clock_duration_ms"] = result.get("duration_ms")
            result["duration_ms"] = result["bridge_elapsed_ms"]
            result["condition"] = condition
            result["task_id"] = task_id
            result["model_id"] = "gpt-6-luna"
            result["reasoning_effort"] = "max"
            result["agent_error"] = None
        except Exception as exc:
            result = {
                "condition": condition,
                "task_id": task_id,
                "status": "controller_error",
                "natural_completion": False,
                "model_id": "gpt-6-luna",
                "reasoning_effort": "max",
                "error": str(exc)[:3000],
                "bridge_elapsed_ms": round((time.monotonic() - started_at) * 1000),
                "duration_ms": round((time.monotonic() - started_at) * 1000),
            }
        finally:
            server.close()
            await server.wait_closed()

        try:
            await environment.upload_dir(workspace, workdir)
        except Exception as exc:
            result["final_sync_error"] = str(exc)[:1500]
        self._save_result(attempt_dir, result)
        context.metadata = {"agentstress": result}

    async def _handle_bridge(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        environment: BaseEnvironment,
        workspace: Path,
        workdir: str,
        lock: asyncio.Lock,
    ) -> None:
        try:
            request_line = await reader.readline()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode(errors="replace").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = min(int(headers.get("content-length", "0")), 2 * 1024 * 1024)
            request = json.loads((await reader.readexactly(length)).decode())
            if not request_line.startswith(b"POST /exec "):
                response = {"stdout": "", "stderr": "unsupported bridge route", "code": 404}
            else:
                async with lock:
                    await environment.upload_dir(workspace, workdir)
                    if self.options.task_id == "risk-scorer-replay":
                        await self._restore_task_write_access(environment, workdir)
                    timeout_ms = request.get("timeout_ms")
                    exec_options = {}
                    if timeout_ms is not None:
                        exec_options["timeout_sec"] = max(1, math.ceil(int(timeout_ms) / 1000))
                    executed = await environment.exec(
                        str(request.get("command", "")), cwd=workdir, **exec_options
                    )
                    stage = workspace.with_name(f"{workspace.name}.sync")
                    shutil.rmtree(stage, ignore_errors=True)
                    await environment.download_dir(workdir, stage)
                    shutil.rmtree(workspace, ignore_errors=True)
                    stage.rename(workspace)
                    response = {
                        "stdout": executed.stdout or "",
                        "stderr": executed.stderr or "",
                        "code": executed.return_code,
                    }
        except Exception as exc:
            response = {"stdout": "", "stderr": str(exc)[:2500], "code": 1}
        body = json.dumps(response).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _save_result(attempt_dir: Path, result: dict) -> None:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "agentstress.json").write_text(json.dumps(result, indent=2) + "\n")

    @staticmethod
    async def _restore_task_write_access(environment: BaseEnvironment, workdir: str) -> None:
        # Harbor upload tar entries use uid 0; risk-scorer-replay deliberately
        # grants nobody write access only to these source and output paths.
        restored = await environment.exec(
            'for p in /app/parityctl /app/output; do if [ -d "$p" ]; then chown -R nobody:nogroup "$p" || exit $?; fi; done',
            cwd=workdir,
            timeout_sec=30,
            user="root",
        )
        if restored.return_code != 0:
            raise RuntimeError(restored.stderr or restored.stdout or "failed to restore task write ownership")
        checked = await environment.exec(
            "test -w /app/parityctl && test -w /app/output",
            cwd=workdir,
            timeout_sec=10,
            user="nobody",
        )
        if checked.return_code != 0:
            raise RuntimeError(checked.stderr or checked.stdout or "task writable paths are inaccessible to nobody")
