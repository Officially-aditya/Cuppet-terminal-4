"""Harbor adapter for the unmodified Cuppet Desktop runtime process."""

import json
import shlex
import tempfile
from pathlib import Path

from pydantic import Field

from harbor.agents.base import BaseAgent
from harbor.agents.installed.codex import Codex
from harbor.agents.options import AgentOptions
from harbor.models.agent.context import AgentContext


class CuppetOptions(AgentOptions):
    bundle_path: str
    codex_version: str = "0.162.1"
    reasoning_effort: str = "max"
    timeout_sec: int = Field(default=0, ge=0)


class CuppetDesktopAgent(BaseAgent):
    options_model = CuppetOptions
    options: CuppetOptions

    @staticmethod
    def name() -> str:
        return "cuppet-desktop"

    def version(self) -> str:
        return "58270905de9e794f12c6dcf6b26e07a069e462ed"

    async def setup(self, environment):
        # Reuse Harbor's supported, version-pinned Codex installation. Cuppet
        # uses its app-server as a model provider, not its CLI agent loop.
        installer = Codex(
            logs_dir=self.logs_dir / "codex-install",
            model_name=self.model_name,
            version=self.options.codex_version,
            extra_env=self.extra_env,
        )
        await installer.setup(environment)
        await environment.upload_file(Path(self.options.bundle_path).resolve(), "/tmp/cuppet-runtime.tar.gz")
        result = await environment.exec(
            command="mkdir -p /opt/cuppet && tar -xzf /tmp/cuppet-runtime.tar.gz -C /opt/cuppet && chmod +x /opt/cuppet/vendor/tst/linux-x64-gnu/tst-daemon",
            user="root",
        )
        if result.return_code:
            raise RuntimeError(f"Cuppet installation failed: {result.stderr}")

    async def run(self, instruction, environment, context: AgentContext):
        auth_path = Path(self._get_env("CODEX_AUTH_JSON_PATH") or "~/.codex/auth.json").expanduser()
        if not auth_path.is_file():
            raise ValueError("Sign in with ChatGPT using codex login before running preflight.")
        identity = await environment.exec(command="id -u")
        owner = (identity.stdout or "").strip()
        if identity.return_code or not owner.isdecimal():
            raise RuntimeError("Could not determine the container's agent UID.")
        home = "/tmp/cuppet-codex-home"
        logs = self.environment_logs_dir.as_posix()
        quote = shlex.quote
        await environment.exec(command=f"mkdir -p {home} {quote(logs)}", user="root")
        await environment.upload_file(auth_path, f"{home}/auth.json")
        with tempfile.TemporaryDirectory() as temporary:
            prompt_path = Path(temporary) / "instruction.txt"
            prompt_path.write_text(instruction)
            await environment.upload_file(prompt_path, "/tmp/cuppet-instruction.txt")
            config_path = Path(temporary) / "config.toml"
            config_path.write_text('web_search = "disabled"\n')
            await environment.upload_file(config_path, f"{home}/config.toml")
        await environment.exec(
            command=f"chmod 700 {home} && chmod 600 {home}/auth.json && chown -R {quote(owner)} {home}",
            user="root",
        )
        try:
            result = await environment.exec(
                command=(
                    "unset OPENAI_API_KEY CODEX_API_KEY OPENAI_BASE_URL; "
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    "node /opt/cuppet/cuppet-run.mjs /opt/cuppet /tmp/cuppet-instruction.txt "
                    f'{quote(logs)} "$PWD" {quote(self.model_name)} '
                    f"{quote(self.options.reasoning_effort)} {self.options.timeout_sec} full"
                ),
                env={"CODEX_HOME": home},
                timeout_sec=self.options.timeout_sec + 60 if self.options.timeout_sec else None,
            )
            (self.logs_dir / "cuppet-launch.log").write_text((result.stdout or "") + (result.stderr or ""))
            if result.return_code:
                raise RuntimeError(f"Cuppet runtime failed ({result.return_code}); see cuppet-launch.log and cuppet-stderr.log.")
            summary_result = await environment.exec(command=f"cat {quote(logs + '/cuppet-result.json')}")
            if summary_result.return_code:
                raise RuntimeError("Cuppet did not produce a task result.")
            summary = json.loads(summary_result.stdout)
            if summary["status"] != "complete":
                raise RuntimeError(f"Cuppet task status: {summary['status']}")
            usage = (summary.get("sample") or {}).get("usage") or {}
            context.n_input_tokens = usage.get("inputTokens")
            context.n_output_tokens = usage.get("outputTokens")
            context.n_cache_tokens = usage.get("cachedInputTokens")
            context.metadata = {
                "cuppet_revision": self.version(),
                "model": self.model_name,
                "reasoning_effort": self.options.reasoning_effort,
                "tst_mode": summary["tst"]["mode"],
                "tst_connected": summary["tst"]["connected"],
                "project_root": summary["projectRoot"],
                "execution": (summary.get("sample") or {}).get("execution"),
            }
        finally:
            # Keep subscription credentials outside exported logs and remove
            # them before Harbor invokes the verifier.
            await environment.exec(command=f"rm -rf {home}", user="root")
