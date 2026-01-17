"""
Celery tasks for backup operations.

Provides asynchronous tasks for account syncing, garbage collection,
token refresh, and health monitoring.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=3,
)
def sync_account_task(
    self,
    account_id: int,
    sync_root_id: int | None = None,
    force_initial: bool = False,
):
    """
    Sync a cloud storage account.

    Args:
        account_id: Account ID to sync
        sync_root_id: Optional specific sync root ID
        force_initial: Force initial sync even if cursor exists
    """
    from backup.models import Account, SyncRoot
    from backup.providers.google_drive import GoogleDriveClient
    from backup.storage import AccountStorage
    from backup.sync import SyncEngine

    try:
        account = Account.objects.get(id=account_id, is_active=True)
    except Account.DoesNotExist:
        logger.warning(f"Account {account_id} not found or inactive")
        return {"status": "skipped", "reason": "account_not_found"}

    logger.info(f"Starting sync for account {account.id} ({account.email})")

    # Get sync roots
    if sync_root_id:
        try:
            sync_roots = [
                SyncRoot.objects.get(
                    id=sync_root_id,
                    account=account,
                    is_enabled=True,
                )
            ]
        except SyncRoot.DoesNotExist:
            logger.warning(f"Sync root {sync_root_id} not found or not enabled")
            return {"status": "skipped", "reason": "sync_root_not_found"}
    else:
        sync_roots = list(account.sync_roots.filter(is_enabled=True))

    if not sync_roots:
        logger.info(f"No enabled sync roots for account {account_id}")
        return {"status": "skipped", "reason": "no_sync_roots"}

    # Create client and storage
    client = GoogleDriveClient(account)
    storage = AccountStorage(account)

    results = []
    for sync_root in sync_roots:
        logger.info(f"Syncing root: {sync_root.name}")

        if force_initial:
            sync_root.sync_cursor = ""
            sync_root.save()

        engine = SyncEngine(
            sync_root=sync_root,
            storage=storage,
            client=client,
        )

        try:
            result = engine.run_sync()
            results.append({
                "sync_root_id": sync_root.id,
                "sync_root_name": sync_root.name,
                "files_added": result.files_added,
                "files_updated": result.files_updated,
                "files_deleted": result.files_deleted,
                "files_quarantined": result.files_quarantined,
                "bytes_downloaded": result.bytes_downloaded,
                "errors": len(result.errors),
            })
        except Exception as e:
            logger.error(f"Sync failed for {sync_root.name}: {e}", exc_info=True)
            results.append({
                "sync_root_id": sync_root.id,
                "sync_root_name": sync_root.name,
                "error": str(e),
            })
            # Re-raise to trigger retry
            raise

    return {
        "status": "completed",
        "account_id": account_id,
        "results": results,
    }


@shared_task
def sync_all_accounts():
    """
    Sync all active accounts.

    Schedules individual sync tasks for each enabled account.
    """
    from backup.models import Account

    accounts = Account.objects.filter(is_active=True)
    scheduled = 0

    for account in accounts:
        if account.sync_roots.filter(is_enabled=True).exists():
            sync_account_task.delay(account.id)
            scheduled += 1
            logger.info(f"Scheduled sync for account {account.id}")

    logger.info(f"Scheduled syncs for {scheduled} accounts")
    return {"scheduled": scheduled}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def run_gc_task(self, account_id: int | None = None):
    """
    Run garbage collection.

    Args:
        account_id: Optional specific account ID (None = all accounts)
    """
    from backup.gc import GarbageCollector
    from backup.models import Account

    account = None
    if account_id:
        try:
            account = Account.objects.get(id=account_id)
        except Account.DoesNotExist:
            logger.warning(f"Account {account_id} not found for GC")
            return {"status": "skipped", "reason": "account_not_found"}

    logger.info(f"Starting garbage collection (account={account_id})")

    gc = GarbageCollector(account=account)
    result = gc.run()

    return {
        "status": "completed",
        "versions_purged": result.versions_purged,
        "blobs_deleted": result.blobs_deleted,
        "quarantine_purged": result.quarantine_purged,
        "bytes_freed": result.bytes_freed,
    }


@shared_task
def refresh_expiring_tokens(hours_threshold: int = 1):
    """
    Proactively refresh tokens expiring within threshold.

    Args:
        hours_threshold: Refresh tokens expiring within this many hours
    """
    from backup.models import Account
    from backup import secrets
    from backup.providers.google_drive import GoogleDriveClient, TokenExpiredError

    now = timezone.now()
    threshold = now + timedelta(hours=hours_threshold)

    results = {"refreshed": 0, "failed": 0, "skipped": 0}

    for account in Account.objects.filter(is_active=True):
        tokens = secrets.get_tokens(account)
        if not tokens or not tokens.get("expires_at"):
            results["skipped"] += 1
            continue

        if tokens["expires_at"] < threshold:
            try:
                client = GoogleDriveClient(account)
                if client.refresh_token_if_needed():
                    results["refreshed"] += 1
                    logger.info(f"Refreshed token for {account.email}")
                else:
                    results["skipped"] += 1
            except TokenExpiredError:
                results["failed"] += 1
                logger.warning(f"Token refresh failed for {account.email}")
        else:
            results["skipped"] += 1

    logger.info(f"Token refresh complete: {results}")
    return results


@shared_task
def sync_due_accounts():
    """Sync accounts whose next_sync_at has passed."""
    from backup.models import Account

    now = timezone.now()

    scheduled = 0
    with transaction.atomic():
        due_accounts = Account.objects.filter(
            is_active=True,
            sync_interval_minutes__gt=0,
            next_sync_at__lte=now,
        ).select_for_update(skip_locked=True)

        for account in due_accounts:
            sync_account_task.delay(account.id)

            account.next_sync_at = now + timedelta(minutes=account.sync_interval_minutes)
            account.save(update_fields=['next_sync_at'])
            scheduled += 1
            logger.info(f"Scheduled sync for account {account.id}, next at {account.next_sync_at}")

    return {"scheduled": scheduled}


@shared_task
def check_account_health():
    """Check health of all accounts and log issues."""
    from backup.models import Account
    from backup import secrets
    from backup.sync.models import SyncSession

    now = timezone.now()
    issues = []

    for account in Account.objects.filter(is_active=True):
        account_issues = []

        # Check token status
        tokens = secrets.get_tokens(account)
        if not tokens:
            account_issues.append("missing_tokens")
        elif tokens.get("expires_at") and tokens["expires_at"] < now:
            account_issues.append("expired_token")

        # Check sync recency (no sync in 24+ hours)
        last_session = SyncSession.objects.filter(
            sync_root__account=account,
            status="completed"
        ).order_by("-completed_at").first()

        if last_session:
            if last_session.completed_at < now - timedelta(hours=24):
                account_issues.append("stale_sync")
        else:
            account_issues.append("never_synced")

        # Check for recent failures
        recent_failures = SyncSession.objects.filter(
            sync_root__account=account,
            status="failed",
            started_at__gte=now - timedelta(hours=24)
        ).count()

        if recent_failures >= 3:
            account_issues.append("repeated_failures")

        if account_issues:
            issues.append({
                "account_id": account.id,
                "email": account.email,
                "issues": account_issues,
            })
            logger.warning(f"Health issues for {account.email}: {account_issues}")

    checked = Account.objects.filter(is_active=True).count()
    logger.info(f"Health check complete: {checked} accounts checked, {len(issues)} with issues")

    return {
        "checked": checked,
        "issues": issues,
    }
