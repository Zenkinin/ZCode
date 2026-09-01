from __future__ import annotations
import json
import os
from pathlib import Path


def _path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".config")
    return Path(base) / "ZCode" / "security.json"


class SecurityPolicy:
    """Permanent shell approvals scoped by workspace and risk type."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._file = _path()
        try:
            self._data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
    @property
    def key(self) -> str:
        return str(self.workspace).casefold()

    def risks(self) -> list[str]:
        return sorted(self._data.get(self.key, []))

    def allows(self, risk: str) -> bool:
        return risk in self._data.get(self.key, [])

    def grant(self, risk: str) -> None:
        self._data[self.key] = sorted(set(self.risks()) | {risk})
        self._save()

    def revoke(self, risk: str | None = None) -> None:
        if risk is None:
            self._data.pop(self.key, None)
        else:
            remaining = [item for item in self.risks() if item != risk]
            if remaining:
                self._data[self.key] = remaining
            else:
                self._data.pop(self.key, None)
        self._save()

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError:
            pass
