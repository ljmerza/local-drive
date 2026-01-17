"""
Status dashboard view for monitoring account health.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from backup import secrets
from backup.models import Account, Provider
from backup.providers.google_drive import GoogleDriveClient, TokenExpiredError

logger = logging.getLogger(__name__)

# Cache connection test results for 5 minutes
CONNECTION_CACHE_TTL = 300


@dataclass
class AccountStatus:
    """Status information for a single account."""

    account: Account
    token_status: Literal["valid", "expiring", "expired", "missing", "unknown"]
    token_expires_at: datetime | None
    connection_status: Literal["connected", "error", "unchecked"]
    connection_error: str | None
    last_sync: datetime | None
    item_count: int
    storage_used: int


def _get_token_status(account: Account) -> tuple[str, datetime | None]:
    """Check token status for an account."""
    tokens = secrets.get_tokens(account)
    if not tokens:
        return "missing", None

    expires_at = tokens.get("expires_at")
    if not expires_at:
        return "unknown", None

    now = datetime.now(timezone.utc)
    if expires_at < now:
        return "expired", expires_at

    # Expiring if less than 1 hour left
    if expires_at < now + timedelta(hours=1):
        return "expiring", expires_at

    return "valid", expires_at


def _test_connection(account: Account) -> tuple[str, str | None]:
    """
    Test connection to provider API.

    Results are cached for 5 minutes to avoid rate limiting.
    """
    cache_key = f"connection_status:{account.id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not secrets.has_tokens(account):
        result = ("error", "No tokens configured")
        cache.set(cache_key, result, CONNECTION_CACHE_TTL)
        return result

    try:
        if account.provider == Provider.GOOGLE_DRIVE:
            client = GoogleDriveClient(account)
            user_info = client.get_user_info()
            result = ("connected", None)
        else:
            result = ("unchecked", f"Provider {account.provider} not supported")
    except TokenExpiredError as e:
        result = ("error", f"Token expired: {e}")
    except Exception as e:
        logger.warning(f"Connection test failed for account {account.id}: {e}")
        result = ("error", str(e))

    cache.set(cache_key, result, CONNECTION_CACHE_TTL)
    return result


def _get_account_status(account: Account, test_connection: bool = False) -> AccountStatus:
    """Build status object for an account."""
    token_status, token_expires = _get_token_status(account)

    if test_connection and token_status in ("valid", "expiring"):
        conn_status, conn_error = _test_connection(account)
    else:
        conn_status, conn_error = "unchecked", None

    # Get last sync from sync roots
    sync_roots = account.sync_roots.filter(is_enabled=True).order_by("-last_sync_at")
    last_sync = sync_roots.first().last_sync_at if sync_roots.exists() else None

    # Get item count
    item_count = sum(sr.items.count() for sr in account.sync_roots.all())

    # Get storage used (sum of blob sizes)
    storage_used = account.blobs.aggregate(total=models.Sum("size_bytes"))["total"] or 0

    return AccountStatus(
        account=account,
        token_status=token_status,
        token_expires_at=token_expires,
        connection_status=conn_status,
        connection_error=conn_error,
        last_sync=last_sync,
        item_count=item_count,
        storage_used=storage_used,
    )


# Need to import models for aggregation
from django.db import models


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """
    Render the status dashboard.

    Shows all accounts with their connection status, token health,
    and sync information.
    """
    accounts = Account.objects.filter(is_active=True).order_by("provider", "email")

    # Check if user wants to test connections (adds latency)
    test_connections = request.GET.get("test") == "1"

    account_statuses = [
        _get_account_status(account, test_connection=test_connections)
        for account in accounts
    ]

    # Summary stats
    total_accounts = len(account_statuses)
    healthy_count = sum(1 for s in account_statuses if s.token_status == "valid")
    expiring_count = sum(1 for s in account_statuses if s.token_status == "expiring")
    expired_count = sum(1 for s in account_statuses if s.token_status in ("expired", "missing"))

    context = {
        "accounts": account_statuses,
        "total_accounts": total_accounts,
        "healthy_count": healthy_count,
        "expiring_count": expiring_count,
        "expired_count": expired_count,
        "test_connections": test_connections,
    }

    return render(request, "backup/dashboard.html", context)
