"""
Secrets manager for storing OAuth tokens outside the database.

Tokens are stored in a JSON file with restricted permissions (600).
This keeps sensitive credentials out of the database entirely.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    from backup.models import Account

logger = logging.getLogger(__name__)


class SecretsError(Exception):
    """Base exception for secrets operations."""

    pass


class SecretsFileError(SecretsError):
    """Raised when secrets file operations fail."""

    pass


class TokenNotFoundError(SecretsError):
    """Raised when tokens for an account are not found."""

    pass


def _get_secrets_path() -> Path:
    """Get the path to the secrets file."""
    return Path(settings.SECRETS_FILE)


def _get_account_key(account: "Account") -> str:
    """
    Generate the key for an account in the secrets file.

    Format: {provider}:{email}
    """
    return f"{account.provider}:{account.email}"


def _load_secrets() -> dict:
    """
    Load secrets from the secrets file.

    Returns:
        Dict of account secrets, empty dict if file doesn't exist
    """
    path = _get_secrets_path()

    if not path.exists():
        return {}

    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in secrets file: {e}")
        raise SecretsFileError(f"Invalid secrets file format: {e}") from e
    except OSError as e:
        logger.error(f"Failed to read secrets file: {e}")
        raise SecretsFileError(f"Failed to read secrets file: {e}") from e


def _save_secrets(data: dict) -> None:
    """
    Save secrets to the secrets file atomically.

    Uses atomic write (temp file + rename) and sets permissions to 600.
    """
    path = _get_secrets_path()

    try:
        # Ensure parent directory exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first (atomic write pattern)
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=".secrets_",
            suffix=".tmp",
        )

        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)

            # Set restrictive permissions before rename
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600

            # Atomic rename
            os.replace(tmp_path, path)

        except Exception:
            # Clean up temp file on error
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except OSError as e:
        logger.error(f"Failed to save secrets file: {e}")
        raise SecretsFileError(f"Failed to save secrets file: {e}") from e


def get_tokens(account: "Account") -> dict | None:
    """
    Get tokens for an account.

    Args:
        account: The Account instance

    Returns:
        Dict with access_token, refresh_token, expires_at or None if not found
    """
    secrets = _load_secrets()
    key = _get_account_key(account)

    tokens = secrets.get(key)
    if tokens is None:
        return None

    # Parse expires_at back to datetime if present
    if "expires_at" in tokens and tokens["expires_at"]:
        try:
            tokens["expires_at"] = datetime.fromisoformat(tokens["expires_at"])
        except (ValueError, TypeError):
            tokens["expires_at"] = None

    return tokens


def set_tokens(
    account: "Account",
    access_token: str,
    refresh_token: str,
    expires_at: datetime | None = None,
) -> None:
    """
    Store tokens for an account.

    Args:
        account: The Account instance
        access_token: OAuth access token
        refresh_token: OAuth refresh token
        expires_at: Token expiration datetime
    """
    secrets = _load_secrets()
    key = _get_account_key(account)

    secrets[key] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }

    _save_secrets(secrets)
    logger.info(f"Saved tokens for account {key}")


def delete_tokens(account: "Account") -> bool:
    """
    Delete tokens for an account.

    Args:
        account: The Account instance

    Returns:
        True if tokens were deleted, False if not found
    """
    secrets = _load_secrets()
    key = _get_account_key(account)

    if key not in secrets:
        return False

    del secrets[key]
    _save_secrets(secrets)
    logger.info(f"Deleted tokens for account {key}")
    return True


def has_tokens(account: "Account") -> bool:
    """Check if tokens exist for an account."""
    secrets = _load_secrets()
    key = _get_account_key(account)
    return key in secrets


def list_accounts() -> list[str]:
    """
    List all account keys in the secrets file.

    Returns:
        List of account keys (format: provider:email)
    """
    secrets = _load_secrets()
    # Filter out the oauth_clients key
    return [k for k in secrets.keys() if k != "oauth_clients"]


def get_oauth_client_config(provider: str) -> dict | None:
    """
    Get OAuth client configuration for a provider.

    Args:
        provider: Provider name (e.g., 'google')

    Returns:
        Dict with client_id, client_secret, redirect_uri or None if not found
    """
    secrets = _load_secrets()
    oauth_clients = secrets.get("oauth_clients", {})
    return oauth_clients.get(provider)


def set_oauth_client_config(
    provider: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str | None = None,
) -> None:
    """
    Store OAuth client configuration for a provider.

    Args:
        provider: Provider name (e.g., 'google')
        client_id: OAuth client ID
        client_secret: OAuth client secret
        redirect_uri: OAuth redirect URI (optional)
    """
    secrets = _load_secrets()

    if "oauth_clients" not in secrets:
        secrets["oauth_clients"] = {}

    secrets["oauth_clients"][provider] = {
        "client_id": client_id,
        "client_secret": client_secret,
    }

    if redirect_uri:
        secrets["oauth_clients"][provider]["redirect_uri"] = redirect_uri

    _save_secrets(secrets)
    logger.info(f"Saved OAuth client config for provider {provider}")
