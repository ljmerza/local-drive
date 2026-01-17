"""
Garbage collection for backup storage.

Handles cleanup of old versions, orphaned blobs, and quarantined items
according to retention policies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

if TYPE_CHECKING:
    from backup.models import Account

from backup.models import BackupBlob, BackupItem, FileVersion, ItemState, RetentionPolicy
from backup.storage import AccountStorage

logger = logging.getLogger(__name__)


@dataclass
class GCResult:
    """Result of a garbage collection run."""

    versions_purged: int = 0
    blobs_deleted: int = 0
    quarantine_purged: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)


class GarbageCollector:
    """
    Garbage collector for backup storage.

    Implements retention policies to clean up:
    1. Old file versions beyond keep_last_n and keep_days thresholds
    2. Orphaned blobs (no longer referenced by any FileVersion)
    3. Quarantined items older than keep_days
    """

    def __init__(
        self,
        account: Account | None = None,
        dry_run: bool = False,
        batch_size: int | None = None,
    ):
        """
        Initialize the garbage collector.

        Args:
            account: Specific account to run GC for (None = all accounts)
            dry_run: If True, report what would be deleted without actually deleting
            batch_size: Number of items to process per batch
        """
        self.account = account
        self.dry_run = dry_run
        self.batch_size = batch_size or getattr(settings, "GC_BATCH_SIZE", 100)

    def run(self) -> GCResult:
        """
        Execute garbage collection.

        Returns:
            GCResult with statistics about the cleanup
        """
        result = GCResult()

        logger.info(
            f"Starting garbage collection (dry_run={self.dry_run}, account={self.account})"
        )

        # Purge old versions
        versions_result = self._purge_old_versions()
        result.versions_purged = versions_result

        # Delete orphaned blobs
        blobs_result = self._delete_orphaned_blobs()
        result.blobs_deleted = blobs_result["count"]
        result.bytes_freed = blobs_result["bytes"]

        # Purge quarantined items
        quarantine_result = self._purge_quarantined_items()
        result.quarantine_purged = quarantine_result

        logger.info(
            f"Garbage collection complete: "
            f"{result.versions_purged} versions purged, "
            f"{result.blobs_deleted} blobs deleted ({result.bytes_freed:,} bytes freed), "
            f"{result.quarantine_purged} quarantined items purged"
        )

        return result

    def _get_retention_policy(self, account: Account) -> tuple[int, int]:
        """
        Get retention policy for an account.

        Returns:
            Tuple of (keep_last_n, keep_days)
        """
        # Try to find account-specific policy
        policy = RetentionPolicy.objects.filter(account=account, sync_root=None).first()
        if policy:
            return policy.keep_last_n, policy.keep_days

        # Fall back to defaults
        return (
            getattr(settings, "GC_DEFAULT_KEEP_VERSIONS", 10),
            getattr(settings, "GC_DEFAULT_KEEP_DAYS", 30),
        )

    def _purge_old_versions(self) -> int:
        """
        Purge file versions beyond retention thresholds.

        Keeps max(keep_last_n, versions within keep_days) for each BackupItem.

        Returns:
            Number of versions purged
        """
        from backup.models import Account

        total_purged = 0
        accounts = [self.account] if self.account else Account.objects.all()

        for account in accounts:
            keep_n, keep_days = self._get_retention_policy(account)
            cutoff_date = timezone.now() - timedelta(days=keep_days)

            logger.debug(
                f"Purging versions for account {account.id}: "
                f"keep_n={keep_n}, keep_days={keep_days}"
            )

            # Get all backup items for this account
            items = BackupItem.objects.filter(sync_root__account=account)

            processed = 0
            for item in items.iterator():
                # Get all versions for this item, ordered by captured_at desc
                versions = list(
                    FileVersion.objects.filter(backup_item=item)
                    .order_by("-captured_at")
                    .values_list("id", "captured_at", flat=False)
                )

                if len(versions) <= keep_n:
                    continue

                # Find versions to delete: beyond keep_n AND older than cutoff
                versions_to_delete = []
                for i, (version_id, captured_at) in enumerate(versions):
                    if i >= keep_n and captured_at < cutoff_date:
                        versions_to_delete.append(version_id)

                if versions_to_delete:
                    if self.dry_run:
                        logger.info(
                            f"[DRY RUN] Would delete {len(versions_to_delete)} versions "
                            f"for item {item.id}"
                        )
                    else:
                        FileVersion.objects.filter(id__in=versions_to_delete).delete()
                        logger.debug(
                            f"Deleted {len(versions_to_delete)} versions for item {item.id}"
                        )
                    total_purged += len(versions_to_delete)

                processed += 1
                if processed % self.batch_size == 0:
                    logger.debug(f"Processed {processed} items...")

        return total_purged

    def _delete_orphaned_blobs(self) -> dict:
        """
        Delete blobs that are no longer referenced by any FileVersion.

        Returns:
            Dict with 'count' and 'bytes' freed
        """
        # Find orphaned blobs
        orphaned_query = BackupBlob.objects.annotate(
            version_count=Count("versions")
        ).filter(version_count=0)

        if self.account:
            orphaned_query = orphaned_query.filter(account=self.account)

        orphaned_blobs = list(orphaned_query.values_list("digest", "size_bytes", "account_id"))

        if not orphaned_blobs:
            return {"count": 0, "bytes": 0}

        total_bytes = sum(size for _, size, _ in orphaned_blobs)

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would delete {len(orphaned_blobs)} orphaned blobs "
                f"({total_bytes:,} bytes)"
            )
            return {"count": len(orphaned_blobs), "bytes": total_bytes}

        # Delete blobs from storage and database
        from backup.models import Account

        deleted_count = 0
        bytes_freed = 0

        for digest, size_bytes, account_id in orphaned_blobs:
            try:
                account = Account.objects.get(id=account_id)
                storage = AccountStorage(account)

                # Delete from filesystem
                if storage.delete_blob(digest):
                    bytes_freed += size_bytes

                # Delete from database
                BackupBlob.objects.filter(digest=digest).delete()
                deleted_count += 1

                logger.debug(f"Deleted orphaned blob: {digest[:20]}...")

            except Exception as e:
                logger.warning(f"Failed to delete blob {digest}: {e}")

        return {"count": deleted_count, "bytes": bytes_freed}

    def _purge_quarantined_items(self) -> int:
        """
        Purge quarantined items older than keep_days.

        Returns:
            Number of items purged
        """
        from backup.models import Account

        total_purged = 0
        accounts = [self.account] if self.account else Account.objects.all()

        for account in accounts:
            _, keep_days = self._get_retention_policy(account)
            cutoff_date = timezone.now() - timedelta(days=keep_days)

            # Find quarantined items older than cutoff
            quarantined_items = BackupItem.objects.filter(
                sync_root__account=account,
                state=ItemState.QUARANTINED,
                state_changed_at__lt=cutoff_date,
            )

            count = quarantined_items.count()

            if count > 0:
                if self.dry_run:
                    logger.info(
                        f"[DRY RUN] Would purge {count} quarantined items "
                        f"for account {account.id}"
                    )
                else:
                    # Delete archive files
                    storage = AccountStorage(account)
                    for item in quarantined_items.iterator():
                        try:
                            archive_path = storage.archive_dir / item.path
                            if archive_path.exists():
                                archive_path.unlink()
                        except Exception as e:
                            logger.warning(
                                f"Failed to delete archive file {item.path}: {e}"
                            )

                    # Update state to PURGED
                    quarantined_items.update(
                        state=ItemState.PURGED,
                        state_changed_at=timezone.now(),
                    )
                    logger.info(f"Purged {count} quarantined items for account {account.id}")

                total_purged += count

        return total_purged
