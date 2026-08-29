from __future__ import annotations

import getpass
from collections.abc import Callable

import keyring
from keyring.errors import KeyringError
from rich.console import Console


SERVICE_NAME = "ZCode"
ACCOUNT_NAME = "deepseek-api-key"


def read_api_key() -> str:
    """Read the API key from the operating system's credential store."""
    try:
        return (keyring.get_password(SERVICE_NAME, ACCOUNT_NAME) or "").strip()
    except KeyringError:
        return ""


def validate_api_key(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise ValueError("API key cannot be empty.")
    if not value.isascii():
        raise ValueError(
            "API key contains a non-ASCII character. Copy the raw key again; "
            "do not include translated text or punctuation."
        )
    if any(character.isspace() for character in value):
        raise ValueError("API key cannot contain spaces or line breaks.")
    return value


def save_api_key(api_key: str) -> None:
    value = validate_api_key(api_key)
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, value)
    except KeyringError as exc:
        raise RuntimeError(f"Could not save API key to the system credential store: {exc}") from exc


def delete_api_key() -> bool:
    if not read_api_key():
        return False
    try:
        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
    except KeyringError as exc:
        raise RuntimeError(
            f"Could not remove API key from the system credential store: {exc}"
        ) from exc
    return True


def prompt_and_save_api_key(
    console: Console,
    *,
    prompt_secret: Callable[[str], str] = getpass.getpass,
) -> str:
    console.print("[cyan]DeepSeek API key setup[/cyan]")
    console.print(
        "[dim]The key is stored in your operating system credential store; "
        "it is not written to this project.[/dim]"
    )
    while True:
        api_key = prompt_secret("DeepSeek API Key: ").strip()
        try:
            save_api_key(api_key)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        console.print("[green]API key saved.[/green]")
        return api_key
