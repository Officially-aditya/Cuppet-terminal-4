"""Harbor's Codex agent with credential ownership matching the container user."""

from harbor.agents.installed.codex import Codex


class SubscriptionCodex(Codex):
    async def run(self, instruction, environment, context):
        identity = await environment.exec(command="id -u")
        uid = (identity.stdout or "").strip()
        if identity.return_code or not uid.isdecimal():
            raise RuntimeError("Could not determine the container's agent UID.")
        # Harbor otherwise assumes an unspecified default_user can read uploaded
        # private files, even when the Docker image declares an unprivileged USER.
        with environment.with_default_user(uid):
            await super().run(instruction, environment, context)
