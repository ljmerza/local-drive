"""
Core sync engine for cloud backup operations.

Orchestrates initial and incremental syncs, managing file downloads,
version tracking, and deletion state transitions per ADR 0001.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from backup.models import Account, SyncRoot
    from backup.providers.google_drive import DriveChange, DriveFile, GoogleDriveClient
    from backup.storage import AccountStorage

from backup.models import BackupBlob, BackupItem, FileVersion, ItemState, ItemType, VersionReason
from backup.providers.google_drive import FileNotDownloadableError
from backup.sync.exceptions import (
    DownloadError,
    StorageError,
    SyncAbortedError,
    TokenRefreshError,
)
from backup.sync.models import SyncEvent, SyncSession
from backup.sync.path_builder import PathBuilder

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    files_added: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    files_quarantined: int = 0
    bytes_downloaded: int = 0
    errors: list[Exception] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class SyncEngine:
    """
    Core sync engine for backing up cloud storage accounts.

    Handles both initial full sync and incremental sync using
    the provider's changes API, with safe deletion handling per ADR 0001.
    """

    def __init__(
        self,
        sync_root: SyncRoot,
        storage: AccountStorage,
        client: GoogleDriveClient,
        batch_size: int = 100,
    ):
        self.sync_root = sync_root
        self.account = sync_root.account
        self.storage = storage
        self.client = client
        self.batch_size = batch_size
        self.session: SyncSession | None = None
        self.path_builder = PathBuilder(sync_root)
        self.sync_start_time = None

    def run_sync(self) -> SyncResult:
        """
        Execute sync operation (initial or incremental).

        Returns:
            SyncResult with statistics about the sync

        Raises:
            SyncAbortedError: If account is disabled or token refresh fails
        """
        # Ensure token is fresh
        try:
            self.client.refresh_token_if_needed()
        except Exception as e:
            raise TokenRefreshError(f"Token refresh failed: {e}") from e

        # Check account is active
        if not self.account.is_active:
            raise SyncAbortedError(f"Account {self.account.id} is disabled")

        # Record sync start time
        self.sync_start_time = timezone.now()

        # Determine sync type
        is_initial = not self.sync_root.sync_cursor or not self.sync_root.last_sync_at

        # Create session
        self.session = SyncSession.objects.create(
            sync_root=self.sync_root,
            is_initial=is_initial,
            start_cursor=self.sync_root.sync_cursor,
        )

        logger.info(
            f"Starting {'initial' if is_initial else 'incremental'} sync for {self.sync_root.name}"
        )

        try:
            if is_initial:
                result = self._run_initial_sync()
            else:
                result = self._run_incremental_sync()

            # Mark session completed
            self.session.status = "completed"
            self.session.completed_at = timezone.now()
            self.session.files_added = result.files_added
            self.session.files_updated = result.files_updated
            self.session.files_deleted = result.files_deleted
            self.session.files_quarantined = result.files_quarantined
            self.session.bytes_downloaded = result.bytes_downloaded
            self.session.save()

            logger.info(
                f"Sync completed: {result.files_added} added, "
                f"{result.files_updated} updated, "
                f"{result.files_deleted} deleted, "
                f"{result.files_quarantined} quarantined"
            )

            return result

        except Exception as e:
            # Mark session failed
            self.session.status = "failed"
            self.session.error_message = str(e)
            self.session.completed_at = timezone.now()
            self.session.save()

            logger.error(f"Sync failed: {e}", exc_info=True)
            raise

    def _run_initial_sync(self) -> SyncResult:
        """
        Perform initial full sync for new accounts.

        Downloads all files without inferring deletions (per ADR 0001).
        """
        logger.info("Running initial sync")
        result = SyncResult()

        # Get the current page token (represents "now")
        start_token = self.client.get_start_page_token()
        logger.info(f"Start page token: {start_token}")

        # Fetch all changes from token "1" to start_token
        # This gives us all current files as "additions"
        for changes, current_token in self.client.iter_all_changes(start_token="1"):
            # Filter out deletions (per ADR: ignore deletions in initial sync)
            file_changes = [c for c in changes if not c.removed]

            logger.debug(
                f"Processing batch of {len(file_changes)} file additions (filtered {len(changes) - len(file_changes)} deletions)"
            )

            # Process this batch
            batch_result = self._process_change_batch(file_changes, current_token, is_initial=True)
            result.files_added += batch_result.files_added
            result.files_updated += batch_result.files_updated
            result.bytes_downloaded += batch_result.bytes_downloaded
            result.errors.extend(batch_result.errors)

        # Save final cursor
        self.sync_root.sync_cursor = start_token
        self.sync_root.last_sync_at = timezone.now()
        self.sync_root.save()

        self.session.end_cursor = start_token
        self.session.save()

        logger.info(f"Initial sync complete, saved cursor: {start_token}")

        return result

    def _run_incremental_sync(self) -> SyncResult:
        """
        Perform incremental sync using saved cursor.

        Applies changes and manages deletion states per ADR 0001.
        """
        logger.info(f"Running incremental sync from cursor: {self.sync_root.sync_cursor}")
        result = SyncResult()

        # Fetch changes from saved cursor
        for changes, current_token in self.client.iter_all_changes(
            start_token=self.sync_root.sync_cursor
        ):
            logger.debug(f"Processing batch of {len(changes)} changes")

            # Process this batch
            batch_result = self._process_change_batch(changes, current_token, is_initial=False)
            result.files_added += batch_result.files_added
            result.files_updated += batch_result.files_updated
            result.files_deleted += batch_result.files_deleted
            result.bytes_downloaded += batch_result.bytes_downloaded
            result.errors.extend(batch_result.errors)

        # After all changes processed, update deletion states
        quarantined = self._update_deletion_states()
        result.files_quarantined = quarantined

        # Save final cursor
        last_token = self.session.end_cursor or self.sync_root.sync_cursor
        self.sync_root.sync_cursor = last_token
        self.sync_root.last_sync_at = timezone.now()
        self.sync_root.save()

        logger.info(f"Incremental sync complete, saved cursor: {last_token}")

        return result

    def _process_change_batch(
        self,
        changes: list[DriveChange],
        current_token: str,
        is_initial: bool = False,
    ) -> SyncResult:
        """
        Process a batch of changes with transaction safety.

        Args:
            changes: List of changes to process
            current_token: Current change token for checkpointing
            is_initial: Whether this is part of initial sync

        Returns:
            SyncResult for this batch
        """
        result = SyncResult()

        for change in changes:
            try:
                # Each change in its own transaction
                with transaction.atomic():
                    item_result = self._process_file_change(change, is_initial)

                    if item_result:
                        result.files_added += item_result.get("added", 0)
                        result.files_updated += item_result.get("updated", 0)
                        result.files_deleted += item_result.get("deleted", 0)
                        result.bytes_downloaded += item_result.get("bytes", 0)

            except DownloadError as e:
                # Log but continue with other files
                logger.warning(f"Download failed for {change.file_id}: {e}")
                SyncEvent.objects.create(
                    session=self.session,
                    event_type="error",
                    provider_file_id=change.file_id,
                    message=str(e),
                )
                result.errors.append(e)

            except Exception as e:
                # Unexpected error, log and continue
                logger.error(f"Unexpected error processing {change.file_id}: {e}", exc_info=True)
                SyncEvent.objects.create(
                    session=self.session,
                    event_type="error",
                    provider_file_id=change.file_id,
                    message=str(e),
                )
                result.errors.append(e)

        # Save checkpoint
        self._save_checkpoint(current_token)

        return result

    def _process_file_change(
        self,
        change: DriveChange,
        is_initial: bool = False,
    ) -> dict | None:
        """
        Process a single file change.

        Args:
            change: The change event
            is_initial: Whether this is part of initial sync

        Returns:
            Dictionary with statistics or None if skipped
        """
        # Handle deletion
        if change.removed or (change.file and change.file.trashed):
            return self._process_file_deleted(change)

        # Handle file addition/update
        if change.file:
            # Skip folders for now (we'll handle them implicitly via parent paths)
            if change.file.mime_type == "application/vnd.google-apps.folder":
                return self._process_folder(change.file)

            # Process regular file
            return self._process_file_added_or_updated(change.file, is_initial)

        return None

    def _process_folder(self, drive_file: DriveFile) -> dict | None:
        """
        Process a folder (create BackupItem but don't download).

        Args:
            drive_file: The folder to process

        Returns:
            Statistics dictionary
        """
        # Build path
        path = self.path_builder.build_path(drive_file)

        # Create or update BackupItem
        item, created = BackupItem.objects.update_or_create(
            sync_root=self.sync_root,
            provider_item_id=drive_file.id,
            defaults={
                "name": drive_file.name,
                "path": path,
                "item_type": ItemType.FOLDER,
                "mime_type": drive_file.mime_type,
                "provider_modified_at": drive_file.modified_time,
                "state": ItemState.ACTIVE,
                "last_seen_at": self.sync_start_time,
            },
        )

        # Create folder on filesystem
        folder_path = self.storage.get_current_path(path)
        folder_path.mkdir(parents=True, exist_ok=True)

        if created:
            logger.debug(f"Created folder: {path}")
            SyncEvent.objects.create(
                session=self.session,
                event_type="file_added",
                backup_item=item,
                provider_file_id=drive_file.id,
                file_path=path,
                message=f"Folder created: {drive_file.name}",
            )
            return {"added": 1}
        else:
            logger.debug(f"Updated folder: {path}")
            return {"updated": 1}

    def _process_file_added_or_updated(
        self,
        drive_file: DriveFile,
        is_initial: bool = False,
    ) -> dict | None:
        """
        Process a file addition or update.

        Args:
            drive_file: The file to process
            is_initial: Whether this is part of initial sync

        Returns:
            Statistics dictionary
        """
        # Build path
        path = self.path_builder.build_path(drive_file)

        # Check if item exists
        try:
            item = BackupItem.objects.get(
                sync_root=self.sync_root,
                provider_item_id=drive_file.id,
            )
            is_new = False

            # Check if content has changed
            content_changed = (
                item.etag != (drive_file.etag or "")
                or item.provider_modified_at != drive_file.modified_time
            )

        except BackupItem.DoesNotExist:
            item = None
            is_new = True
            content_changed = True

        # Download and store if content changed
        digest = None
        bytes_downloaded = 0

        if content_changed and drive_file.is_downloadable:
            try:
                digest = self._download_and_store(drive_file)
                bytes_downloaded = drive_file.size or 0
            except (DownloadError, StorageError) as e:
                logger.warning(f"Failed to download {drive_file.name}: {e}")
                if is_new:
                    # For new files, skip if download fails
                    raise
                # For updates, continue with existing data
                digest = None

        # Create or update BackupItem
        if is_new:
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id=drive_file.id,
                name=drive_file.name,
                path=path,
                item_type=ItemType.FILE,
                mime_type=drive_file.mime_type,
                size_bytes=drive_file.size,
                provider_modified_at=drive_file.modified_time,
                etag=drive_file.etag or "",
                state=ItemState.ACTIVE,
                last_seen_at=self.sync_start_time,
            )

            logger.info(f"Added file: {path}")
            SyncEvent.objects.create(
                session=self.session,
                event_type="file_added",
                backup_item=item,
                provider_file_id=drive_file.id,
                file_path=path,
                message=f"File added: {drive_file.name}",
            )

        else:
            # Update existing item
            item.name = drive_file.name
            item.path = path
            item.mime_type = drive_file.mime_type
            item.size_bytes = drive_file.size
            item.provider_modified_at = drive_file.modified_time
            item.etag = drive_file.etag or ""
            item.last_seen_at = self.sync_start_time

            # Reset deletion tracking if file reappears
            if item.state != ItemState.ACTIVE:
                logger.info(f"File reappeared: {path} (was {item.state})")
                item.state = ItemState.ACTIVE
                item.missing_since_sync_count = 0

            item.save()

            if content_changed:
                logger.info(f"Updated file: {path}")
                SyncEvent.objects.create(
                    session=self.session,
                    event_type="file_updated",
                    backup_item=item,
                    provider_file_id=drive_file.id,
                    file_path=path,
                    message=f"File updated: {drive_file.name}",
                )

        # Create FileVersion if we downloaded content
        if digest:
            try:
                blob = BackupBlob.objects.get(digest=digest)
                FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path=path,
                    etag_or_revision=drive_file.etag,
                    content_modified_at=drive_file.modified_time,
                    reason=VersionReason.UPDATE,
                )

                # Materialize to current/ directory
                self.storage.materialize_to_current(digest, path)

            except BackupBlob.DoesNotExist:
                logger.error(f"Blob {digest} not found after download")

        if is_new:
            return {"added": 1, "bytes": bytes_downloaded}
        elif content_changed:
            return {"updated": 1, "bytes": bytes_downloaded}
        else:
            return {}

    def _process_file_deleted(self, change: DriveChange) -> dict:
        """
        Process an explicit file deletion.

        Args:
            change: The deletion change event

        Returns:
            Statistics dictionary
        """
        try:
            item = BackupItem.objects.get(
                sync_root=self.sync_root,
                provider_item_id=change.file_id,
            )
        except BackupItem.DoesNotExist:
            # File we never tracked, ignore
            return {}

        # Create pre_delete version if we have content
        if item.item_type == ItemType.FILE and item.versions.exists():
            latest_version = item.versions.latest("captured_at")
            FileVersion.objects.create(
                account=self.account,
                backup_item=item,
                blob=latest_version.blob,
                observed_path=item.path,
                etag_or_revision=item.etag,
                content_modified_at=item.provider_modified_at,
                reason=VersionReason.PRE_DELETE,
            )

        # Move to archive
        if item.item_type == ItemType.FILE:
            try:
                self.storage.move_to_archive(item.path)
            except Exception as e:
                logger.warning(f"Failed to move {item.path} to archive: {e}")

        # Update state
        item.state = ItemState.DELETED_UPSTREAM
        item.state_changed_at = timezone.now()
        item.missing_since_sync_count = 0
        item.save()

        logger.info(f"Deleted file: {item.path}")
        SyncEvent.objects.create(
            session=self.session,
            event_type="file_deleted",
            backup_item=item,
            provider_file_id=change.file_id,
            file_path=item.path,
            message="File deleted upstream",
        )

        return {"deleted": 1}

    def _download_and_store(self, drive_file: DriveFile) -> str:
        """
        Download file content and store as blob.

        Args:
            drive_file: File to download

        Returns:
            The digest of stored content

        Raises:
            DownloadError: If download fails
            StorageError: If storage write fails
        """
        if not drive_file.is_downloadable:
            raise DownloadError(f"File type {drive_file.mime_type} cannot be downloaded")

        try:
            # Download file content
            content = BytesIO()
            self.client.download_file_to_stream(drive_file.id, content)
            content.seek(0)

            # Write to blob storage
            data = content.read()
            digest = self.storage.write_blob(data)

            # Create or get BackupBlob record
            BackupBlob.objects.get_or_create(
                digest=digest,
                defaults={
                    "account": self.account,
                    "size_bytes": len(data),
                },
            )

            logger.debug(f"Downloaded and stored {drive_file.name}: {digest[:20]}...")
            return digest

        except FileNotDownloadableError as e:
            raise DownloadError(str(e)) from e
        except Exception as e:
            raise StorageError(f"Failed to store file: {e}") from e

    def _update_deletion_states(self) -> int:
        """
        Update deletion state machine for files not seen in this sync.

        Implements the 2-sync threshold logic from ADR 0001.

        Returns:
            Count of newly quarantined items
        """
        # Find all ACTIVE items not seen in this sync
        missing_items = BackupItem.objects.filter(
            sync_root=self.sync_root,
            state=ItemState.ACTIVE,
            last_seen_at__lt=self.sync_start_time,
        )

        quarantined_count = 0

        for item in missing_items:
            # Increment missing counter
            item.missing_since_sync_count += 1

            if item.missing_since_sync_count >= 2:
                # Transition to QUARANTINED

                # 1. Create pre_delete FileVersion if we have content
                if item.item_type == ItemType.FILE and item.versions.exists():
                    latest_version = item.versions.latest("captured_at")
                    FileVersion.objects.create(
                        account=self.account,
                        backup_item=item,
                        blob=latest_version.blob,
                        observed_path=item.path,
                        etag_or_revision=item.etag,
                        content_modified_at=item.provider_modified_at,
                        reason=VersionReason.PRE_DELETE,
                    )

                # 2. Move file to archive
                if item.item_type == ItemType.FILE:
                    try:
                        self.storage.move_to_archive(item.path)
                    except Exception as e:
                        logger.warning(f"Failed to move {item.path} to archive: {e}")

                # 3. Update item state
                item.state = ItemState.QUARANTINED
                item.state_changed_at = timezone.now()
                quarantined_count += 1

                logger.info(
                    f"Quarantined file: {item.path} (missing for {item.missing_since_sync_count} syncs)"
                )
                SyncEvent.objects.create(
                    session=self.session,
                    event_type="file_quarantined",
                    backup_item=item,
                    file_path=item.path,
                    message=f"Missing for {item.missing_since_sync_count} consecutive syncs",
                )

            else:
                # Transition to MISSING_UPSTREAM
                if item.state == ItemState.ACTIVE:
                    item.state = ItemState.MISSING_UPSTREAM
                    item.state_changed_at = timezone.now()
                    logger.debug(f"File missing: {item.path} (count: {item.missing_since_sync_count})")

            item.save()

        logger.info(f"Quarantined {quarantined_count} files")
        return quarantined_count

    def _save_checkpoint(self, cursor: str) -> None:
        """
        Save progress checkpoint.

        Args:
            cursor: Current sync cursor
        """
        self.session.end_cursor = cursor
        self.session.save(update_fields=["end_cursor"])

        SyncEvent.objects.create(
            session=self.session,
            event_type="checkpoint",
            message=f"Checkpoint: cursor={cursor[:20]}...",
        )

        logger.debug(f"Saved checkpoint: {cursor[:20]}...")
