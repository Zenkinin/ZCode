from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from zcode.core.types import Message, Role, ToolCall


class SessionCorrupt(ValueError):
    """Raised when a persisted session cannot be decoded safely."""


DEFAULT_NAME_SOURCE = "default"
DERIVED_NAME_SOURCE = "derived"
USER_NAME_SOURCE = "user"
MAX_SESSION_NAME_LENGTH = 40


def clean_session_name(name: str) -> str:
    """Normalize and validate a user-visible session name."""
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        raise ValueError("Session name cannot be empty")
    if len(cleaned) > MAX_SESSION_NAME_LENGTH:
        raise ValueError(
            f"Session name must be at most {MAX_SESSION_NAME_LENGTH} characters"
        )
    return cleaned


def derive_session_name(task: str, max_length: int = 28) -> str:
    """Derive a stable short title locally from the first user task."""
    cleaned = " ".join(task.split()).strip()
    cleaned = re.sub(
        r"^(?:请(?:你)?|麻烦(?:你)?|你可以|能否|可以)?\s*(?:帮我)?\s*",
        "",
        cleaned,
    )
    cleaned = cleaned.rstrip("。！？!?.,， ") or "新会话"
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + "…"
    return cleaned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_to_dict(message: Message) -> dict[str, object]:
    return {
        "role": message.role.value,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "reasoning_content": message.reasoning_content,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
                "raw_arguments": call.raw_arguments,
                "parse_error": call.parse_error,
            }
            for call in message.tool_calls
        ],
    }


def _message_from_dict(data: dict[str, object]) -> Message:
    try:
        calls = [
            ToolCall(
                id=str(item["id"]),
                name=str(item["name"]),
                arguments=dict(item.get("arguments", {})),
                raw_arguments=str(item.get("raw_arguments", "{}")),
                parse_error=(
                    str(item["parse_error"])
                    if item.get("parse_error") is not None
                    else None
                ),
            )
            for item in data.get("tool_calls", [])
        ]
        return Message(
            role=Role(str(data["role"])),
            content=data.get("content") if isinstance(data.get("content"), str) else None,
            tool_call_id=(
                str(data["tool_call_id"])
                if data.get("tool_call_id") is not None
                else None
            ),
            tool_calls=calls,
            reasoning_content=(
                str(data["reasoning_content"])
                if data.get("reasoning_content") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionCorrupt(f"Invalid message record: {exc}") from exc


@dataclass(slots=True)
class SessionSnapshot:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    name_source: str = DEFAULT_NAME_SOURCE
    cwd: str = "."
    messages: list[Message] = field(default_factory=list)
    plan: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    name: str
    updated_at: str
    cwd: str


class SessionStore:
    """JSONL-backed sessions scoped to one workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root / ".zcode" / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def create(self, name: str | None = None) -> SessionSnapshot:
        session_id = f"s-{uuid4().hex[:8]}"
        clean_name = clean_session_name(name) if name and name.strip() else None
        snapshot = SessionSnapshot(
            session_id=session_id,
            name=clean_name or f"session-{session_id[2:]}",
            created_at=_now(),
            updated_at=_now(),
            name_source=USER_NAME_SOURCE if clean_name else DEFAULT_NAME_SOURCE,
        )
        self.save(snapshot, active=True)
        return snapshot

    def save(self, snapshot: SessionSnapshot, *, active: bool = False) -> None:
        snapshot.updated_at = _now()
        path = self._path(snapshot.session_id)
        temporary = path.with_suffix(".jsonl.tmp")
        records = [
            {
                "type": "meta",
                "session_id": snapshot.session_id,
                "name": snapshot.name,
                "name_source": snapshot.name_source,
                "created_at": snapshot.created_at,
                "updated_at": snapshot.updated_at,
                "cwd": snapshot.cwd,
            }
        ]
        records.extend(
            {"type": "message", "message": _message_to_dict(message)}
            for message in snapshot.messages
        )
        records.append({"type": "plan", "steps": snapshot.plan})
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        if active:
            self._write_index(snapshot.session_id)

    def load(self, session_id: str) -> SessionSnapshot:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        try:
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionCorrupt(f"Could not read session {session_id}: {exc}") from exc
        if not records or records[0].get("type") != "meta":
            raise SessionCorrupt(f"Session {session_id} has no metadata record")
        meta = records[0]
        try:
            snapshot = SessionSnapshot(
                session_id=str(meta["session_id"]),
                name=str(meta["name"]),
                created_at=str(meta["created_at"]),
                updated_at=str(meta["updated_at"]),
                name_source=str(
                    meta.get(
                        "name_source",
                        DEFAULT_NAME_SOURCE
                        if str(meta["name"]).startswith("session-")
                        else USER_NAME_SOURCE,
                    )
                ),
                cwd=str(meta.get("cwd", ".")),
            )
            for record in records[1:]:
                if record.get("type") == "message":
                    snapshot.messages.append(_message_from_dict(record["message"]))
                elif record.get("type") == "plan":
                    steps = record.get("steps", [])
                    if not isinstance(steps, list):
                        raise TypeError("plan steps must be a list")
                    snapshot.plan = [dict(step) for step in steps]
            return snapshot
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SessionCorrupt):
                raise
            raise SessionCorrupt(f"Invalid session {session_id}: {exc}") from exc

    def load_active(self) -> SessionSnapshot | None:
        active_id = self._active_id()
        if active_id:
            try:
                return self.load(active_id)
            except FileNotFoundError:
                pass
        summaries = self.list()
        if not summaries:
            return None
        return self.load(max(summaries, key=lambda item: item.updated_at).session_id)

    def list(self) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        for path in sorted(self.root.glob("s-*.jsonl")):
            try:
                first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                if first.get("type") != "meta":
                    continue
                summaries.append(
                    SessionSummary(
                        session_id=str(first["session_id"]),
                        name=str(first["name"]),
                        updated_at=str(first["updated_at"]),
                        cwd=str(first.get("cwd", ".")),
                    )
                )
            except (OSError, IndexError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)

    def set_active(self, session_id: str) -> None:
        self.load(session_id)
        self._write_index(session_id)

    def _path(self, session_id: str) -> Path:
        if not session_id.startswith("s-") or "/" in session_id or "\\" in session_id:
            raise ValueError("Invalid session id")
        return self.root / f"{session_id}.jsonl"

    def _active_id(self) -> str | None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            value = data.get("active_session_id")
            return str(value) if value else None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def _write_index(self, session_id: str) -> None:
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"active_session_id": session_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)


def plan_to_records(steps: list[object]) -> list[dict[str, object]]:
    return [
        {
            "id": int(step.id),
            "description": step.description,
            "status": step.status.value,
        }
        for step in steps
    ]
