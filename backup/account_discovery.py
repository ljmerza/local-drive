"""
Auto-discovery of accounts from secrets file.

Scans secrets.json for token entries and creates Account records
if they don't already exist.
"""

import logging
from typing import List, Tuple

from django.db import transaction

from backup.models import Account, SyncRoot
from backup.secrets import list_accounts as list_secret_accounts

logger = logging.getLogger(__name__)


class DiscoveryResult:
    """Result of account discovery."""

    def __init__(self):
        self.created_accounts: List[Tuple[str, str]] = []  # [(provider, email), ...]
        self.existing_accounts: List[Tuple[str, str]] = []
        self.errors: List[str] = []

    @property
    def total_found(self) -> int:
        return len(self.created_accounts) + len(self.existing_accounts)

    @property
    def created_count(self) -> int:
        return len(self.created_accounts)


def discover_accounts() -> DiscoveryResult:
    """
    Discover accounts from secrets file and create missing Account records.

    Returns:
        DiscoveryResult with details of what was created/found
    """
    result = DiscoveryResult()

    # Get all account keys from secrets file
    # Format: "provider:email" (e.g., "google:user@gmail.com")
    try:
        account_keys = list_secret_accounts()
    except Exception as e:
        logger.error(f"Failed to read secrets file: {e}")
        result.errors.append(f"Failed to read secrets file: {e}")
        return result

    if not account_keys:
        logger.info("No accounts found in secrets file")
        return result

    logger.info(f"Found {len(account_keys)} account(s) in secrets file")

    # Process each account
    for account_key in account_keys:
        try:
            # Parse account key: "provider:email"
            parts = account_key.split(":", 1)
            if len(parts) != 2:
                error_msg = f"Invalid account key format: {account_key}"
                logger.warning(error_msg)
                result.errors.append(error_msg)
                continue

            provider_str, email = parts

            # Map provider string to Provider enum
            # secrets.json uses "google", model uses "google_drive"
            provider_map = {
                "google": "google_drive",
                "google_drive": "google_drive",
                "onedrive": "onedrive",
            }

            provider = provider_map.get(provider_str.lower())
            if not provider:
                error_msg = f"Unknown provider: {provider_str}"
                logger.warning(error_msg)
                result.errors.append(error_msg)
                continue

            # Check if account already exists
            existing = Account.objects.filter(
                provider=provider,
                email=email,
            ).first()

            if existing:
                logger.debug(f"Account already exists: {provider} / {email}")
                result.existing_accounts.append((provider, email))
                continue

            # Create new account
            with transaction.atomic():
                account = Account.objects.create(
                    provider=provider,
                    email=email,
                    name=f"{email} ({provider.replace('_', ' ').title()})",
                    is_active=True,
                )

                # Create default sync root for Google Drive
                if provider == "google_drive":
                    SyncRoot.objects.create(
                        account=account,
                        provider_root_id="root",  # Google Drive "My Drive" root
                        name="My Drive",
                        is_enabled=True,
                    )

                logger.info(f"Created account: {provider} / {email} (ID: {account.id})")
                result.created_accounts.append((provider, email))

        except Exception as e:
            error_msg = f"Failed to process {account_key}: {e}"
            logger.error(error_msg)
            result.errors.append(error_msg)

    return result
