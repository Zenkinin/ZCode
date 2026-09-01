from zcode.security import SecurityPolicy


def test_security_policy_persists_per_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "config"))
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()

    first = SecurityPolicy(first_workspace)
    first.grant("recursive deletion")

    assert SecurityPolicy(first_workspace).allows("recursive deletion")
    assert not SecurityPolicy(second_workspace).allows("recursive deletion")

    first.revoke("recursive deletion")
    assert not SecurityPolicy(first_workspace).allows("recursive deletion")
