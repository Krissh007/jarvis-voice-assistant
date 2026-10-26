"""Starting and stopping the Agent Hub from JARVIS (the Command centre's buttons, and on demand).

The Hub ships `scripts/start_all.ps1` (config `agent_hub.start_script`): it starts JobPilot's API, its MCP
server, the optional connectors and the Hub, each in the background with its own log, waiting for each
to answer before the next; `-Stop` stops only what the script started. JARVIS runs that script and
nothing else: the path comes from config, never from a page or the model.

One run at a time: a button press and an automatic start share the same run. After a failed start, an
automatic start waits START_RETRY_S before trying again (the buttons can always try).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from core.privacy import secret_in_text

log = logging.getLogger(__name__)

START_TIMEOUT_S = 300  # the script waits for each service in turn
STOP_TIMEOUT_S = 60
OUTPUT_LINES = 12      # of the script's output, kept for the page
START_RETRY_S = 120    # after a failed start, automatic starts wait this long


def script_from(config: dict) -> Path | None:
    """agent_hub.start_script, if it's a PowerShell script that exists."""
    spec = str((config.get("agent_hub") or {}).get("start_script") or "").strip()
    path = Path(spec) if spec else None
    return path if path is not None and path.suffix.lower() == ".ps1" and path.is_file() else None


class HubLauncher:
    def __init__(self, script: Path | None, status: Callable[[str], None] | None = None) -> None:
        self.script = script
        self.status = status or (lambda text: None)   # a short line for every view ("Agent Hub: starting")
        self.running: str | None = None                # "start" / "stop" while the script runs
        self._lock = asyncio.Lock()
        self._failed_at = 0.0
        self._last: dict = {}                          # the latest run's answer, shared with a second asker

    @property
    def available(self) -> bool:
        return self.script is not None

    async def run(self, action: str) -> dict:
        """{"ok", "message", "lines"}. Never raises."""
        if action not in ("start", "stop"):
            return {"ok": False, "message": "That isn't something the Agent Hub buttons do.", "lines": []}
        if self.script is None:
            return {"ok": False, "message": "No Agent Hub start script is set up (agent_hub.start_script).", "lines": []}
        if self._lock.locked():
            if self.running == action:   # already doing it: wait for that run and share its answer
                async with self._lock:
                    return dict(self._last)
            return {"ok": False, "message": f"The Agent Hub is {'starting' if self.running == 'start' else 'stopping'} "
                                            "right now.", "lines": []}
        async with self._lock:
            self.running = action
            try:
                self._last = await self._run(action)
            finally:
                self.running = None
            if action == "start":
                self._failed_at = 0.0 if self._last["ok"] else time.monotonic()
            return dict(self._last)

    async def ensure_started(self) -> dict | None:
        """For a request that needs the Hub while it's down: start it, unless a start just failed.
        None when there's nothing to try (no script, or too soon after a failed start)."""
        if self.script is None or (self._failed_at and time.monotonic() - self._failed_at < START_RETRY_S):
            return None
        log.info("Agent Hub isn't running: starting it for this request")
        return await self.run("start")

    async def _run(self, action: str) -> dict:
        verb = "starting" if action == "start" else "stopping"
        self.status(f"(Agent Hub: {verb})")
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.script)]
        if action == "stop":
            args.append("-Stop")
        timeout = START_TIMEOUT_S if action == "start" else STOP_TIMEOUT_S
        log.info("Agent Hub %s (%s)", action, self.script.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(self.script.parent.parent), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                message = f"The Agent Hub didn't finish {verb} in {timeout // 60} minutes."
                self.status(f"({message})")
                return {"ok": False, "message": message, "lines": []}
        except OSError as exc:
            log.warning("Agent Hub %s couldn't run: %s", action, exc)
            return {"ok": False, "message": "PowerShell couldn't run the Agent Hub's script.", "lines": []}
        lines = [" ".join(line.split())[:200] for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
        lines = ["(a line was hidden: it looked like a secret)" if secret_in_text(line) else line
                 for line in lines][-OUTPUT_LINES:]
        ok = proc.returncode == 0
        log.info("Agent Hub %s %s (exit %s)", action, "done" if ok else "failed", proc.returncode)
        if action == "start":
            message = "The Agent Hub is running." if ok else "The Agent Hub didn't start: see the lines below."
        else:
            message = "Stopped what the Agent Hub's script had started." if ok else "The Agent Hub didn't stop: see the lines below."
        self.status(f"({message})")
        return {"ok": ok, "message": message, "lines": lines}
