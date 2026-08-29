from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
from typing import Any

from zcode.core.types import ToolDefinition, ToolResult
from zcode.tools.base import Tool
from zcode.workspace import Workspace


def _trim_output(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(1, (limit - 80) // 2)
    omitted = len(value) - (half * 2)
    return f"{value[:half]}\n[... {omitted} characters omitted ...]\n{value[-half:]}"


class RunCommandTool(Tool):
    activity = "running"

    def __init__(
        self,
        workspace: Workspace,
        timeout_seconds: float = 120.0,
        max_output_chars: int = 20_000,
    ) -> None:
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        shell_name = "PowerShell" if os.name == "nt" else "the platform shell"
        self.definition = ToolDefinition(
            name="run_command",
            description=(
                "Run a shell command with the working directory restricted to a directory "
                f"inside the workspace using {shell_name}. Returns exit code, stdout, and stderr. "
                "The supplied cwd is authoritative; do not run cd/pwd merely to confirm it. "
                "This is not an OS sandbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": "."},
                    "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("'command' must be a non-empty string")
        cwd = self.workspace.resolve(str(arguments.get("cwd", ".")), must_exist=True)
        if not cwd.is_dir():
            raise ValueError("'cwd' must be a directory")
        timeout = min(float(arguments.get("timeout_seconds", self.timeout_seconds)), 600.0)

        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        if os.name == "nt":
            utf8_setup = (
                "$utf8 = [System.Text.UTF8Encoding]::new($false); "
                "[Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8; "
            )
            exit_handling = (
                "; $zcodeSucceeded = $?; $zcodeExitCode = $LASTEXITCODE; "
                "if ($null -ne $zcodeExitCode) { exit $zcodeExitCode }; "
                "if (-not $zcodeSucceeded) { exit 1 }"
            )
            process = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                utf8_setup + "& { " + command + " }" + exit_handling,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            output_encoding = "utf-8"
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=start_new_session,
            )
            output_encoding = locale.getpreferredencoding(False)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            await self._terminate_process_tree(process)
            return ToolResult(
                False,
                f"Command timed out after {timeout:.1f}s and was terminated.",
                error_code="command_timeout",
            )

        stdout = stdout_bytes.decode(output_encoding, errors="replace")
        stderr = stderr_bytes.decode(output_encoding, errors="replace")
        content = (
            f"exit_code: {process.returncode}\n"
            f"stdout:\n{stdout or '(empty)'}\n"
            f"stderr:\n{stderr or '(empty)'}"
        )
        content = _trim_output(content, self.max_output_chars)
        return ToolResult(
            process.returncode == 0,
            content,
            error_code=None if process.returncode == 0 else "command_failed",
            metadata={"exit_code": process.returncode},
        )

    @staticmethod
    async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()
