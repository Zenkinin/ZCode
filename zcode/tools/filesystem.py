from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from zcode.core.types import ToolDefinition, ToolResult
from zcode.tools.base import Tool
from zcode.workspace import Workspace


def _required_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{name}' must be a non-empty string")
    return value


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"File is not valid UTF-8 text: {path.name}") from exc


class ListDirectoryTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.definition = ToolDefinition(
            name="list_directory",
            description="List direct children of a directory inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative directory path."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve(_required_string(arguments, "path"), must_exist=True)
        if not path.is_dir():
            raise ValueError(f"Not a directory: {self.workspace.relative(path)}")
        entries = sorted(path.iterdir(), key=lambda item: item.name.lower())
        lines: list[str] = []
        for item in entries:
            is_link = item.is_symlink() or (
                hasattr(item, "is_junction") and item.is_junction()
            )
            if is_link:
                kind = "link"
            elif item.is_dir():
                kind = "dir "
            else:
                kind = "file"
            lines.append(f"{kind} {self.workspace.lexical_relative(item)}")
        return ToolResult(True, "\n".join(lines) if lines else "(empty directory)")


class ReadFileTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace, max_lines: int = 400) -> None:
        self.workspace = workspace
        self.max_lines = max_lines
        self.definition = ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file or a bounded line range inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve(_required_string(arguments, "path"), must_exist=True)
        if not path.is_file():
            raise ValueError(f"Not a file: {self.workspace.relative(path)}")
        lines = _read_utf8(path).splitlines()
        start = int(arguments.get("start_line", 1))
        requested_end = int(arguments.get("end_line", start + self.max_lines - 1))
        if start < 1 or requested_end < start:
            raise ValueError("Invalid line range")
        end = min(requested_end, start + self.max_lines - 1, len(lines))
        selected = lines[start - 1 : end]
        body = "\n".join(f"{number:>5} | {line}" for number, line in enumerate(selected, start))
        suffix = ""
        if requested_end > end or end < len(lines):
            suffix = f"\n[showing lines {start}-{end} of {len(lines)}]"
        return ToolResult(True, body + suffix, metadata={"line_count": len(lines)})


class SearchTextTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace, max_results: int = 100) -> None:
        self.workspace = workspace
        self.max_results = max_results
        self.definition = ToolDefinition(
            name="search_text",
            description="Search UTF-8 files for a literal text string inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "*"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        query = _required_string(arguments, "query")
        root = self.workspace.resolve(str(arguments.get("path", ".")), must_exist=True)
        pattern = str(arguments.get("glob", "*"))
        candidates = [root] if root.is_file() else root.rglob(pattern)
        matches: list[str] = []
        for path in candidates:
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    matches.append(f"{self.workspace.relative(path)}:{number}: {line.strip()}")
                    if len(matches) >= self.max_results:
                        return ToolResult(True, "\n".join(matches) + "\n[results truncated]")
        return ToolResult(True, "\n".join(matches) if matches else "No matches found.")


class EditFileTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.definition = ToolDefinition(
            name="edit_file",
            description=(
                "Replace one exact, unique text block in an existing UTF-8 file. "
                "The edit is rejected if old_text is absent or appears more than once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve(_required_string(arguments, "path"), must_exist=True)
        old_text = _required_string(arguments, "old_text")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            raise ValueError("'new_text' must be a string")
        before = _read_utf8(path)
        count = before.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found; read the latest file before editing")
        if count > 1:
            raise ValueError(f"old_text appears {count} times; provide a more specific block")
        after = before.replace(old_text, new_text, 1)
        self.workspace.snapshot(path)
        path.write_text(after, encoding="utf-8")
        self.workspace.note_change()
        relative = self.workspace.relative(path)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        return ToolResult(True, diff, metadata={"path": relative, "changed": True})


class WriteFileTool(Tool):
    activity = "executing"

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.definition = ToolDefinition(
            name="write_file",
            description="Create or fully replace a UTF-8 text file inside the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        path = self.workspace.resolve(_required_string(arguments, "path"))
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("'content' must be a string")
        before = _read_utf8(path) if path.exists() else ""
        if before == content:
            return ToolResult(True, "No change: file already has the requested content.")
        self.workspace.snapshot(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.workspace.note_change()
        relative = self.workspace.relative(path)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        return ToolResult(True, diff, metadata={"path": relative, "changed": True})
