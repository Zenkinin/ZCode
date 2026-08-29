from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a path escapes the configured workspace."""


@dataclass(slots=True)
class Workspace:
    root: Path
    cwd: Path = field(init=False)
    _originals: dict[Path, str | None] = field(default_factory=dict, init=False)
    _revision: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {self.root}")
        self.cwd = self.root

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def cwd_relative(self) -> str:
        relative = self.cwd.relative_to(self.root).as_posix()
        return relative or "."

    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        resolved = candidate.resolve(strict=must_exist)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"Path escapes workspace: {path}") from exc
        if any(
            part.lower() in {".git", ".venv", ".zcode"} for part in relative.parts
        ):
            raise WorkspaceViolation(f"Path is in a protected workspace directory: {path}")
        return resolved

    def change_directory(self, path: str | Path) -> Path:
        destination = self.resolve(path, must_exist=True)
        if not destination.is_dir():
            raise ValueError(f"Not a directory: {path}")
        self.cwd = destination
        return destination

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def lexical_relative(self, path: Path) -> str:
        """Display an entry path without following a symlink or junction target."""
        return path.absolute().relative_to(self.root).as_posix()

    def begin_task(self) -> None:
        self._originals.clear()

    def snapshot(self, path: Path) -> None:
        path = path.resolve()
        if path in self._originals:
            return
        self._originals[path] = path.read_text(encoding="utf-8") if path.exists() else None

    def note_change(self) -> None:
        self._revision += 1

    def diff(self) -> str:
        chunks: list[str] = []
        for path, original in self._originals.items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            before = (original or "").splitlines(keepends=True)
            after = (current or "").splitlines(keepends=True)
            if before == after:
                continue
            relative = self.relative(path)
            chunks.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
        return "".join(chunks)

    def undo(self) -> list[str]:
        restored: list[str] = []
        for path, original in reversed(list(self._originals.items())):
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(original, encoding="utf-8")
            restored.append(self.relative(path))
        if restored:
            self.note_change()
        self._originals.clear()
        return restored
