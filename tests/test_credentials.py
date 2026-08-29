from __future__ import annotations

from rich.console import Console
import pytest

from zcode import credentials


def test_read_save_and_delete_api_key(monkeypatch):
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        credentials.keyring,
        "get_password",
        lambda service, account: stored.get((service, account)),
    )
    monkeypatch.setattr(
        credentials.keyring,
        "set_password",
        lambda service, account, value: stored.__setitem__((service, account), value),
    )
    monkeypatch.setattr(
        credentials.keyring,
        "delete_password",
        lambda service, account: stored.pop((service, account)),
    )

    assert credentials.read_api_key() == ""
    credentials.save_api_key("  secret-key  ")
    assert credentials.read_api_key() == "secret-key"
    assert credentials.delete_api_key() is True
    assert credentials.delete_api_key() is False


def test_prompt_saves_key_without_echoing_it(monkeypatch):
    saved: list[str] = []
    monkeypatch.setattr(credentials, "save_api_key", saved.append)
    console = Console(record=True)

    value = credentials.prompt_and_save_api_key(
        console, prompt_secret=lambda _: "secret-key"
    )

    assert value == "secret-key"
    assert saved == ["secret-key"]
    assert "secret-key" not in console.export_text()


@pytest.mark.parametrize("value", ["sk-abcdà", "sk-ab cd", "sk-ab\ncd"])
def test_save_rejects_key_that_cannot_be_used_as_http_header(value):
    with pytest.raises(ValueError):
        credentials.save_api_key(value)


def test_prompt_retries_invalid_key_without_echoing_it(monkeypatch):
    stored: list[str] = []
    monkeypatch.setattr(
        credentials.keyring,
        "set_password",
        lambda service, account, value: stored.append(value),
    )
    entered = iter(["sk-bàd", "sk-good"])
    console = Console(record=True)

    value = credentials.prompt_and_save_api_key(
        console, prompt_secret=lambda _: next(entered)
    )

    assert value == "sk-good"
    assert stored == ["sk-good"]
    assert "sk-bàd" not in console.export_text()
