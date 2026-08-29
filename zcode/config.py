from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from zcode.credentials import read_api_key, validate_api_key


@dataclass(frozen=True, slots=True)
class Settings:
    workspace: Path
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    thinking: str = "enabled"
    reasoning_effort: str = "high"
    command_timeout_seconds: float = 120.0
    max_tool_output_chars: int = 20_000
    max_context_chars: int = 180_000
    no_progress_limit: int = 3
    emergency_max_steps: int = 100

    @classmethod
    def from_env(cls, workspace: str | Path | None = None) -> "Settings":
        root = Path(workspace or Path.cwd()).resolve()
        thinking = os.getenv("ZCODE_THINKING", "enabled").strip().lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("ZCODE_THINKING must be 'enabled' or 'disabled'")

        effort = os.getenv("ZCODE_REASONING", "high").strip().lower()
        if effort not in {"low", "high", "max"}:
            raise ValueError("ZCODE_REASONING must be low, high, or max")

        return cls(
            workspace=root,
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip() or read_api_key(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.getenv("ZCODE_MODEL", "deepseek-v4-flash"),
            thinking=thinking,
            reasoning_effort=effort,
            command_timeout_seconds=float(os.getenv("ZCODE_COMMAND_TIMEOUT", "120")),
            max_tool_output_chars=int(os.getenv("ZCODE_MAX_TOOL_OUTPUT", "20000")),
            max_context_chars=int(os.getenv("ZCODE_MAX_CONTEXT_CHARS", "180000")),
            no_progress_limit=int(os.getenv("ZCODE_NO_PROGRESS_LIMIT", "3")),
            emergency_max_steps=int(os.getenv("ZCODE_EMERGENCY_MAX_STEPS", "100")),
        )

    def require_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "DeepSeek API key is not configured. Run 'zcode --configure'."
            )
        try:
            validate_api_key(self.api_key)
        except ValueError as exc:
            raise RuntimeError(
                f"Saved DeepSeek API key is invalid: {exc} "
                "Run 'zcode --configure' to replace it."
            ) from exc
