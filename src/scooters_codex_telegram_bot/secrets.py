from __future__ import annotations

KEYRING_SERVICE = "scooters-codex-telegram-bot"
KEYRING_USERNAME = "telegram-bot-token"


class SecretStoreError(RuntimeError):
    pass


def read_telegram_token() -> str | None:
    try:
        import keyring
        from keyring.errors import KeyringError, NoKeyringError
    except ImportError:
        return None

    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except (KeyringError, NoKeyringError):
        return None


def store_telegram_token(token: str) -> None:
    try:
        import keyring
        from keyring.errors import KeyringError, NoKeyringError
    except ImportError as error:
        raise SecretStoreError("System credential storage is unavailable") from error

    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
    except (KeyringError, NoKeyringError) as error:
        raise SecretStoreError("System credential storage is unavailable") from error
