from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
import re
import shlex
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from zcode.core.types import ToolDefinition, ToolResult
from zcode.tools.base import Tool
from zcode.workspace import Workspace


@dataclass(frozen=True, slots=True)
class CommandRisk:
    level: str
    reason: str = ""


DESTRUCTIVE_PATTERNS = (
    (r"(?i)(?:^|[;&|])\s*rm\s+(?:-[a-z]*f[a-z]*r|-[a-z]*r[a-z]*f)\b", "recursive or forced deletion"),
    (r"(?i)\bremove-item\b[^\r\n]*(?:-recurse|-force)", "recursive or forced deletion"),
    (r"(?i)\bremove-item\b[^\r\n]*-force", "forced deletion"),
    (r"(?i)\b(?:rmdir|rd)\b[^\r\n]*/s\b", "recursive directory deletion"),
    (r"(?i)\bdel\b[^\r\n]*/s\b", "recursive file deletion"),
    (r"(?i)\bgit\s+reset\s+--hard\b", "Git history/worktree reset"),
    (r"(?i)\bgit\s+clean\b[^\r\n]*-[a-z]*f", "forced Git clean"),
    (r"(?i)\bgit\s+checkout\s+--\s+", "discarding file changes"),
    (r"(?i)\bgit\s+(?:restore|checkout)\s+(?:\.|--\s*\.)", "discarding file changes"),
    (r"(?i)\bgit\s+branch\s+-D\b", "force deleting a Git branch"),
    (r"(?i)\bgit\s+push\b[^\r\n]*(?:--force|-f\b)", "forced Git push"),
    (r"(?i)\bgit\s+push\b[^\r\n]*--force-with-lease", "forced Git push"),
    (r"(?i)\bgit\s+rebase\b", "rewriting Git history"),
)


def classify_command(command: str) -> CommandRisk:
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, command):
            return CommandRisk("destructive", reason)
    return CommandRisk("normal")


def has_sensitive_target(command: str, workspace_root: str) -> bool:
    """Return whether a destructive target needs per-command confirmation."""
    lowered = command.casefold()
    if any(token in lowered for token in (".git", ".zcode", ".venv")):
        return True
    if workspace_root.casefold() in lowered:
        return True
    return bool(
        re.search(
            r"(?i)\b(?:remove-item|rm|rmdir|rd|del)\b[^\r\n]*"
            r"(?:^|\s|['\"])(?:\.{1,2}(?:\s|$|['\"/\\])|[a-z]:[\\/]|\\\\|/)",
            command,
        )
    )


def command_approval_scope(
    command: str,
    cwd: str,
    workspace_root: str,
) -> str | None:
    """Return a stable workspace-relative scope when the target is unambiguous."""
    root = os.path.normcase(os.path.abspath(workspace_root))
    working_directory = os.path.normcase(os.path.abspath(cwd))
    if re.search(r"(?i)\bgit\s+", command):
        try:
            if os.path.commonpath((root, working_directory)) != root:
                return None
            relative = os.path.relpath(working_directory, root)
        except ValueError:
            return None
        return f"git:{relative}"

    match = re.search(r"(?i)\b(remove-item|rm|rmdir|rd|del)\b([^;&|\r\n]*)", command)
    if not match:
        return None
    try:
        tokens = shlex.split(match.group(2), posix=False)
    except ValueError:
        return None
    candidates: list[str] = []
    expects_path = False
    for raw in tokens:
        token = raw.strip("'\"").rstrip(",")
        if not token:
            continue
        if token.casefold() in {"-path", "-literalpath"}:
            expects_path = True
            continue
        if token.startswith("-") or token.casefold() in {"/s", "/q", "/f"}:
            continue
        if expects_path or not candidates:
            candidates.append(token)
            expects_path = False
        else:
            # Multiple positional targets cannot be represented by one grant.
            return None
    if len(candidates) != 1 or any(
        char in candidates[0] for char in "*?[],$%`(){}"
    ):
        return None
    target = os.path.normcase(
        os.path.abspath(os.path.join(working_directory, candidates[0]))
    )
    try:
        if os.path.commonpath((root, target)) != root:
            return None
        relative = os.path.relpath(target, root)
    except ValueError:
        return None
    if relative == "." or relative.split(os.sep)[0].casefold() in {
        ".git",
        ".zcode",
        ".venv",
    }:
        return None
    return f"path:{relative}"


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
        confirm_callback: Callable[[str, CommandRisk, str], Awaitable[str]] | None = None,
    ) -> None:
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.confirm_callback = confirm_callback
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
        risk = classify_command(command)
        cwd = self.workspace.resolve(str(arguments.get("cwd", ".")), must_exist=True)
        if not cwd.is_dir():
            raise ValueError("'cwd' must be a directory")
        if risk.level == "destructive" and not arguments.get("_confirmed"):
            decision = "no"
            if self.confirm_callback is not None:
                decision = (await self.confirm_callback(command, risk, str(cwd))).lower()
            if decision not in {"once", "always"}:
                return ToolResult(
                    False,
                    "Destructive command was not approved by the user. "
                    f"Reason: {risk.reason}. Command: {command}",
                    error_code="confirmation_required",
                    metadata={"risk": risk.level, "risk_reason": risk.reason, "decision": "no"},
                )
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
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )
            output_encoding = "utf-8"
        else:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=start_new_session,
                env={
                    **os.environ,
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
            )
            output_encoding = locale.getpreferredencoding(False)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            raise
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
