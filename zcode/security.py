from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


class SecurityPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    risk: str
    scope: str


def _path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".config")
    return Path(base) / "ZCode" / "security.json"


def _approval_id(workspace: str, risk: str, scope: str) -> str:
    digest = hashlib.sha256(f"{workspace}\0{risk}\0{scope}".encode()).hexdigest()[:8]
    return f"p-{digest}"


class SecurityPolicy:
    """Permanent shell approvals scoped by workspace, risk, and target."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._file = _path()
        self.load_warning: str | None = None
        self._data = self._load()

    @property
    def key(self) -> str:
        return str(self.workspace).casefold()

    def approvals(self) -> list[Approval]:
        records = self._data.get("workspaces", {}).get(self.key, [])
        return sorted(
            (
                Approval(str(item["id"]), str(item["risk"]), str(item["scope"]))
                for item in records
                if isinstance(item, dict)
                and all(name in item for name in ("id", "risk", "scope"))
            ),
            key=lambda item: (item.risk, item.scope),
        )

    def allows(self, risk: str, scope: str | None) -> bool:
        return scope is not None and any(
            item.risk == risk and item.scope == scope for item in self.approvals()
        )

    def grant(self, risk: str, scope: str) -> Approval:
        approval = Approval(_approval_id(self.key, risk, scope), risk, scope)
        records = [
            {"id": item.approval_id, "risk": item.risk, "scope": item.scope}
            for item in self.approvals()
            if item.approval_id != approval.approval_id
        ]
        records.append(
            {"id": approval.approval_id, "risk": approval.risk, "scope": approval.scope}
        )
        updated = json.loads(json.dumps(self._data))
        updated.setdefault("workspaces", {})[self.key] = records
        self._save(updated)
        self._data = updated
        return approval

    def revoke(self, approval_id: str | None = None) -> int:
        existing = self.approvals()
        remaining = (
            []
            if approval_id is None
            else [item for item in existing if item.approval_id != approval_id]
        )
        if len(remaining) == len(existing) and approval_id is not None:
            return 0
        updated = json.loads(json.dumps(self._data))
        workspaces = updated.setdefault("workspaces", {})
        if remaining:
            workspaces[self.key] = [
                {"id": item.approval_id, "risk": item.risk, "scope": item.scope}
                for item in remaining
            ]
        else:
            workspaces.pop(self.key, None)
        self._save(updated)
        self._data = updated
        return len(existing) - len(remaining)

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 2, "workspaces": {}}
        except (OSError, ValueError) as exc:
            self.load_warning = f"Could not load shell approvals: {exc}"
            return {"version": 2, "workspaces": {}}
        if not isinstance(data, dict):
            self.load_warning = "Could not load shell approvals: invalid data format"
            return {"version": 2, "workspaces": {}}
        if data.get("version") != 2 or not isinstance(data.get("workspaces"), dict):
            # Version 1 grants were risk-wide. Do not silently retain that broad scope.
            return {"version": 2, "workspaces": {}}
        return data

    def _save(self, data: dict[str, object]) -> None:
        temporary = self._file.with_suffix(".json.tmp")
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temporary.replace(self._file)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecurityPolicyError(f"Could not save shell approval: {exc}") from exc
