"""Tests for garbage collection."""

import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from backup.gc import GarbageCollector
from backup.models import (
    Account,
    BackupBlob,
    BackupItem,
    FileVersion,
    ItemState,
    ItemType,
    Provider,
    RetentionPolicy,
    SyncRoot,
    VersionReason,
)
from backup.storage import AccountStorage


class GCTestCase(TestCase):
    """Base test case with common setup for GC tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.backup_root = Path(self.temp_dir) / "backup_data"

        self.account = Account.objects.create(
            provider=Provider.GOOGLE_DRIVE,
            name="Test Account",
            email="test@example.com",
            is_active=True,
        )

        self.sync_root = SyncRoot.objects.create(
            account=self.account,
            provider_root_id="root",
            name="My Drive",
            is_enabled=True,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_blob(self, content: bytes = b"test content") -> BackupBlob:
        """Create a blob in storage and database."""
        with override_settings(BACKUP_ROOT=self.backup_root):
            storage = AccountStorage(self.account)
            digest = storage.write_blob(content)
            blob, _ = BackupBlob.objects.get_or_create(
                digest=digest,
                defaults={
                    "account": self.account,
                    "size_bytes": len(content),
                },
            )
            return blob


class VersionPurgeTests(GCTestCase):
    """Tests for version purging logic."""

    def test_purge_versions_beyond_keep_n(self):
        """Versions beyond keep_last_n should be purged."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=3,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            # Create 5 versions, all old enough to be purged
            # Note: captured_at has auto_now_add=True, so we must update after creation
            old_time = timezone.now() - timedelta(days=60)
            for i in range(5):
                blob = self._create_blob(f"content{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            # Should keep 3, purge 2
            self.assertEqual(result.versions_purged, 2)
            self.assertEqual(FileVersion.objects.filter(backup_item=item).count(), 3)

    def test_purge_versions_beyond_keep_days(self):
        """Only versions older than keep_days should be purged."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=2,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            # Create 4 versions: 2 old (>30 days), 2 recent
            old_time = timezone.now() - timedelta(days=60)
            recent_time = timezone.now() - timedelta(days=10)

            for i in range(2):
                blob = self._create_blob(f"old{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            for i in range(2):
                blob = self._create_blob(f"recent{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=recent_time + timedelta(hours=i)
                )

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            # 2 recent versions kept (within keep_days), 2 old ones purged
            # Actually, keep_n=2, so we keep the 2 most recent, and purge the 2 old ones
            self.assertEqual(result.versions_purged, 2)
            self.assertEqual(FileVersion.objects.filter(backup_item=item).count(), 2)

    def test_respects_retention_policy(self):
        """GC should respect account-specific retention policies."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=10,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create custom policy with keep_last_n=2
            RetentionPolicy.objects.create(
                account=self.account,
                keep_last_n=2,
                keep_days=7,
            )

            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            # Create 5 old versions
            old_time = timezone.now() - timedelta(days=60)
            for i in range(5):
                blob = self._create_blob(f"content{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            # Should keep 2, purge 3 (per account policy, not default)
            self.assertEqual(result.versions_purged, 3)
            self.assertEqual(FileVersion.objects.filter(backup_item=item).count(), 2)


class OrphanedBlobTests(GCTestCase):
    """Tests for orphaned blob cleanup."""

    def test_orphaned_blob_deleted(self):
        """Blobs with no referencing versions should be deleted."""
        with override_settings(BACKUP_ROOT=self.backup_root):
            # Create an orphaned blob (no FileVersion references it)
            blob = self._create_blob(b"orphaned content")

            # Verify blob exists on disk
            storage = AccountStorage(self.account)
            self.assertTrue(storage.blob_exists(blob.digest))

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            self.assertEqual(result.blobs_deleted, 1)
            self.assertGreater(result.bytes_freed, 0)

            # Blob should be removed from database and disk
            self.assertFalse(BackupBlob.objects.filter(digest=blob.digest).exists())
            self.assertFalse(storage.blob_exists(blob.digest))

    def test_referenced_blob_not_deleted(self):
        """Blobs referenced by versions should not be deleted."""
        with override_settings(BACKUP_ROOT=self.backup_root):
            blob = self._create_blob(b"referenced content")

            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            FileVersion.objects.create(
                account=self.account,
                backup_item=item,
                blob=blob,
                observed_path="test.txt",
                reason=VersionReason.UPDATE,
            )

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            self.assertEqual(result.blobs_deleted, 0)

            # Blob should still exist
            self.assertTrue(BackupBlob.objects.filter(digest=blob.digest).exists())
            storage = AccountStorage(self.account)
            self.assertTrue(storage.blob_exists(blob.digest))


class QuarantinePurgeTests(GCTestCase):
    """Tests for quarantined item purging."""

    def test_quarantined_item_purged(self):
        """Quarantined items older than keep_days should be purged."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create quarantined item older than 30 days
            old_time = timezone.now() - timedelta(days=60)
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.QUARANTINED,
            )
            BackupItem.objects.filter(pk=item.pk).update(state_changed_at=old_time)

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            self.assertEqual(result.quarantine_purged, 1)

            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.PURGED)

    def test_recent_quarantined_item_not_purged(self):
        """Quarantined items within keep_days should not be purged."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create recently quarantined item
            recent_time = timezone.now() - timedelta(days=10)
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.QUARANTINED,
            )
            BackupItem.objects.filter(pk=item.pk).update(state_changed_at=recent_time)

            gc = GarbageCollector(account=self.account)
            result = gc.run()

            self.assertEqual(result.quarantine_purged, 0)

            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.QUARANTINED)


class DryRunTests(GCTestCase):
    """Tests for dry-run mode."""

    def test_dry_run_no_changes(self):
        """Dry run should report but not make changes."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=2,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            # Create 5 old versions
            old_time = timezone.now() - timedelta(days=60)
            for i in range(5):
                blob = self._create_blob(f"content{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            # Create orphaned blob
            orphan = self._create_blob(b"orphaned")

            # Create old quarantined item and set state_changed_at
            quarantined = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file2",
                name="quarantined.txt",
                path="quarantined.txt",
                item_type=ItemType.FILE,
                state=ItemState.QUARANTINED,
            )
            BackupItem.objects.filter(pk=quarantined.pk).update(
                state_changed_at=old_time
            )

            gc = GarbageCollector(account=self.account, dry_run=True)
            result = gc.run()

            # Should report what would be deleted
            self.assertEqual(result.versions_purged, 3)
            self.assertEqual(result.blobs_deleted, 1)
            self.assertEqual(result.quarantine_purged, 1)

            # But nothing should actually be deleted
            self.assertEqual(FileVersion.objects.filter(backup_item=item).count(), 5)
            self.assertTrue(BackupBlob.objects.filter(digest=orphan.digest).exists())
            quarantined.refresh_from_db()
            self.assertEqual(quarantined.state, ItemState.QUARANTINED)


class BatchProcessingTests(GCTestCase):
    """Tests for batch processing behavior."""

    def test_batch_processing(self):
        """GC should process items in batches."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=1,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create many items with versions
            old_time = timezone.now() - timedelta(days=60)
            for i in range(25):
                item = BackupItem.objects.create(
                    sync_root=self.sync_root,
                    provider_item_id=f"file{i}",
                    name=f"test{i}.txt",
                    path=f"test{i}.txt",
                    item_type=ItemType.FILE,
                    state=ItemState.ACTIVE,
                )
                # 3 versions each
                for j in range(3):
                    blob = self._create_blob(f"content{i}{j}".encode())
                    version = FileVersion.objects.create(
                        account=self.account,
                        backup_item=item,
                        blob=blob,
                        observed_path=f"test{i}.txt",
                        reason=VersionReason.UPDATE,
                    )
                    FileVersion.objects.filter(pk=version.pk).update(
                        captured_at=old_time + timedelta(hours=j)
                    )

            gc = GarbageCollector(account=self.account, batch_size=10)
            result = gc.run()

            # Should purge 2 versions per item (keep 1, purge 2)
            self.assertEqual(result.versions_purged, 50)  # 25 items * 2 versions


class MultiAccountTests(GCTestCase):
    """Tests for multi-account GC behavior."""

    def test_gc_for_specific_account(self):
        """GC should only affect specified account."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=2,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create second account
            account2 = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test Account 2",
                email="test2@example.com",
                is_active=True,
            )
            sync_root2 = SyncRoot.objects.create(
                account=account2,
                provider_root_id="root",
                name="My Drive 2",
                is_enabled=True,
            )

            # Create items for both accounts
            old_time = timezone.now() - timedelta(days=60)

            item1 = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )
            for i in range(5):
                blob = self._create_blob(f"content1_{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item1,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            item2 = BackupItem.objects.create(
                sync_root=sync_root2,
                provider_item_id="file2",
                name="test2.txt",
                path="test2.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )
            for i in range(5):
                blob2 = BackupBlob.objects.create(
                    digest=f"sha256:{'b' * 62}{i:02d}",
                    account=account2,
                    size_bytes=100,
                )
                version = FileVersion.objects.create(
                    account=account2,
                    backup_item=item2,
                    blob=blob2,
                    observed_path="test2.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            # Run GC for account 1 only
            gc = GarbageCollector(account=self.account)
            result = gc.run()

            # Should only purge from account 1
            self.assertEqual(result.versions_purged, 3)
            self.assertEqual(
                FileVersion.objects.filter(backup_item=item1).count(), 2
            )
            # Account 2 should be unchanged
            self.assertEqual(
                FileVersion.objects.filter(backup_item=item2).count(), 5
            )

    def test_gc_all_accounts(self):
        """GC with no account specified should process all accounts."""
        with override_settings(
            BACKUP_ROOT=self.backup_root,
            GC_DEFAULT_KEEP_VERSIONS=2,
            GC_DEFAULT_KEEP_DAYS=30,
        ):
            # Create second account
            account2 = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test Account 2",
                email="test2@example.com",
                is_active=True,
            )
            sync_root2 = SyncRoot.objects.create(
                account=account2,
                provider_root_id="root",
                name="My Drive 2",
                is_enabled=True,
            )

            old_time = timezone.now() - timedelta(days=60)

            item1 = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )
            for i in range(5):
                blob = self._create_blob(f"content1_{i}".encode())
                version = FileVersion.objects.create(
                    account=self.account,
                    backup_item=item1,
                    blob=blob,
                    observed_path="test.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            item2 = BackupItem.objects.create(
                sync_root=sync_root2,
                provider_item_id="file2",
                name="test2.txt",
                path="test2.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )
            for i in range(5):
                blob2 = BackupBlob.objects.create(
                    digest=f"sha256:{'c' * 62}{i:02d}",
                    account=account2,
                    size_bytes=100,
                )
                version = FileVersion.objects.create(
                    account=account2,
                    backup_item=item2,
                    blob=blob2,
                    observed_path="test2.txt",
                    reason=VersionReason.UPDATE,
                )
                FileVersion.objects.filter(pk=version.pk).update(
                    captured_at=old_time + timedelta(hours=i)
                )

            # Run GC for all accounts
            gc = GarbageCollector()
            result = gc.run()

            # Should purge from both accounts
            self.assertEqual(result.versions_purged, 6)  # 3 from each account
