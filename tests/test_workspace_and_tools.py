from __future__ import annotations

import pytest
import sys

from zcode.tools.filesystem import (
    EditFileTool,
    ListDirectoryTool,
    SearchTextTool,
    WriteFileTool,
)
from zcode.tools.shell import (
    RunCommandTool,
    classify_command,
    command_approval_scope,
    has_sensitive_target,
)
from zcode.tools.navigation import ChangeDirectoryTool
from zcode.workspace import Workspace, WorkspaceViolation


def test_workspace_rejects_parent_escape(tmp_path):
    workspace = Workspace(tmp_path)
    with pytest.raises(WorkspaceViolation):
        workspace.resolve("../secret.txt")


def test_workspace_rejects_internal_directories(tmp_path):
    (tmp_path / ".git").mkdir()
    workspace = Workspace(tmp_path)

    with pytest.raises(WorkspaceViolation, match="protected"):
        workspace.resolve(".git/config")


@pytest.mark.asyncio
async def test_change_directory_persists_for_relative_paths_and_shell(tmp_path):
    target = tmp_path / "OPPO 互联"
    target.mkdir()
    workspace = Workspace(tmp_path)

    changed = await ChangeDirectoryTool(workspace).execute({"path": "OPPO 互联"})
    command = "(Get-Location).Path" if sys.platform == "win32" else "pwd"
    shell_result = await RunCommandTool(workspace, timeout_seconds=5).execute(
        {"command": command}
    )

    assert changed.success
    assert workspace.cwd == target
    assert workspace.cwd_relative == "OPPO 互联"
    assert workspace.resolve("README.md") == target / "README.md"
    assert shell_result.success
    assert str(target) in shell_result.content


@pytest.mark.asyncio
async def test_exact_edit_diff_and_undo(tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    workspace.begin_task()

    result = await EditFileTool(workspace).execute(
        {
            "path": "calc.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
        }
    )

    assert result.success
    assert "+    return a + b" in result.content
    assert "return a + b" in target.read_text(encoding="utf-8")
    assert workspace.diff()
    assert workspace.undo() == ["calc.py"]
    assert "return a - b" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_edit_rejects_ambiguous_old_text(tmp_path):
    target = tmp_path / "values.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    with pytest.raises(ValueError, match="appears 2 times"):
        await EditFileTool(workspace).execute(
            {"path": "values.txt", "old_text": "same", "new_text": "changed"}
        )


@pytest.mark.asyncio
async def test_write_new_file_and_search(tmp_path):
    workspace = Workspace(tmp_path)
    write_result = await WriteFileTool(workspace).execute(
        {"path": "src/app.py", "content": "def hello():\n    return 'hello'\n"}
    )
    search_result = await SearchTextTool(workspace).execute(
        {"path": ".", "query": "hello", "glob": "*.py"}
    )

    assert write_result.success
    assert "src/app.py:1" in search_result.content


@pytest.mark.asyncio
async def test_run_command_captures_output(tmp_path):
    workspace = Workspace(tmp_path)
    executable = f'"{sys.executable}"'
    command = f'& {executable} -c "print(123)"' if sys.platform == "win32" else f'{executable} -c "print(123)"'

    result = await RunCommandTool(workspace, timeout_seconds=5).execute(
        {"command": command}
    )

    assert result.success
    assert result.metadata["exit_code"] == 0
    assert "123" in result.content


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell behavior is Windows-specific")
async def test_run_command_uses_powershell_and_preserves_unicode(tmp_path):
    workspace = Workspace(tmp_path)

    result = await RunCommandTool(workspace, timeout_seconds=5).execute(
        {"command": "Write-Output '中文'; Get-ChildItem -Name"}
    )

    assert result.success
    assert "中文" in result.content
    assert "not recognized" not in result.content


@pytest.mark.asyncio
async def test_run_command_blocks_destructive_command_without_confirmation(tmp_path):
    workspace = Workspace(tmp_path)

    result = await RunCommandTool(workspace, timeout_seconds=5).execute(
        {"command": "Remove-Item -Recurse -Force target"}
    )

    assert not result.success
    assert result.error_code == "confirmation_required"
    assert result.metadata["risk"] == "destructive"


def test_shell_risk_classification_covers_git_and_delete_commands():
    assert classify_command("git reset --hard HEAD").level == "destructive"
    assert classify_command("Remove-Item target -Recurse").level == "destructive"
    assert classify_command("python -m pytest -q").level == "normal"


def test_shell_risk_classification_covers_common_force_variants():
    for command in (
        "rm -rf build",
        "Remove-Item cache -Force",
        "git restore .",
        "git branch -D old-feature",
        "git push --force-with-lease",
        "git rebase main",
    ):
        assert classify_command(command).level == "destructive"


def test_sensitive_shell_targets_always_require_per_command_confirmation(tmp_path):
    root = str(tmp_path)
    for command in (
        "Remove-Item . -Recurse -Force",
        "Remove-Item ../other -Recurse -Force",
        "Remove-Item .git -Recurse -Force",
        r"Remove-Item C:\outside -Recurse -Force",
        f'Remove-Item "{root}" -Recurse -Force',
    ):
        assert has_sensitive_target(command, root)
    assert not has_sensitive_target("Remove-Item build -Recurse -Force", root)


def test_shell_approval_scope_is_bound_to_target_path(tmp_path):
    root = str(tmp_path)
    assert command_approval_scope(
        "Remove-Item build -Recurse -Force", root, root
    ) == "path:build"
    assert command_approval_scope(
        "Remove-Item src -Recurse -Force", root, root
    ) == "path:src"
    assert command_approval_scope("git clean -fd", root, root) == "git:."
    assert command_approval_scope("Remove-Item . -Recurse -Force", root, root) is None
    assert command_approval_scope(
        "Remove-Item ../outside -Recurse -Force", root, root
    ) is None
    assert command_approval_scope(
        "Remove-Item $env:TEMP -Recurse -Force", root, root
    ) is None
    assert command_approval_scope("git clean -fd", str(tmp_path.parent), root) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell behavior is Windows-specific")
async def test_run_command_preserves_chinese_python_argument(tmp_path):
    workspace = Workspace(tmp_path)
    executable = f'& "{sys.executable}"'

    result = await RunCommandTool(workspace, timeout_seconds=5).execute(
        {"command": f'{executable} -c "import sys; print(sys.argv[1])" "任务内容"'}
    )

    assert result.success
    assert "任务内容" in result.content


@pytest.mark.asyncio
async def test_list_directory_does_not_follow_outside_link(tmp_path):
    workspace = Workspace(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory links is unavailable on this system")

    result = await ListDirectoryTool(workspace).execute({"path": "."})

    assert result.success
    assert "link linked-directory" in result.content
