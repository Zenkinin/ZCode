import json

import pytest

from zcode.security import SecurityPolicy, SecurityPolicyError


def test_security_policy_persists_per_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "config"))
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()

    first = SecurityPolicy(first_workspace)
    approval = first.grant("recursive deletion", "path:build")

    assert SecurityPolicy(first_workspace).allows("recursive deletion", "path:build")
    assert not SecurityPolicy(first_workspace).allows("recursive deletion", "path:src")
    assert not SecurityPolicy(second_workspace).allows(
        "recursive deletion", "path:build"
    )

    assert first.revoke(approval.approval_id) == 1
    assert not SecurityPolicy(first_workspace).allows("recursive deletion", "path:build")


def test_security_policy_does_not_keep_failed_grant(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = SecurityPolicy(workspace)

    def fail_save(_data):
        raise SecurityPolicyError("disk full")

    monkeypatch.setattr(policy, "_save", fail_save)
    with pytest.raises(SecurityPolicyError, match="disk full"):
        policy.grant("recursive deletion", "path:build")
    assert not policy.allows("recursive deletion", "path:build")


def test_security_policy_ignores_legacy_risk_wide_grants(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("LOCALAPPDATA", str(config))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    security_file = config / "ZCode" / "security.json"
    security_file.parent.mkdir(parents=True)
    security_file.write_text(
        json.dumps({str(workspace).casefold(): ["recursive deletion"]}),
        encoding="utf-8",
    )

    assert SecurityPolicy(workspace).approvals() == []


def test_security_policy_handles_invalid_top_level_data(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("LOCALAPPDATA", str(config))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    security_file = config / "ZCode" / "security.json"
    security_file.parent.mkdir(parents=True)
    security_file.write_text("[]", encoding="utf-8")

    policy = SecurityPolicy(workspace)

    assert policy.approvals() == []
    assert policy.load_warning == "Could not load shell approvals: invalid data format"
