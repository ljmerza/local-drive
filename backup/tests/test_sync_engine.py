"""Tests for the SyncEngine."""

import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from backup import secrets
from backup.models import (
    Account,
    BackupBlob,
    BackupItem,
    FileVersion,
    ItemState,
    ItemType,
    Provider,
    SyncRoot,
    VersionReason,
)
from backup.providers.google_drive import (
    ChangesPage,
    DriveChange,
    DriveFile,
    FileNotDownloadableError,
    GoogleDriveClient,
)
from backup.storage import AccountStorage
from backup.sync import SyncEngine
from backup.sync.exceptions import DownloadError, SyncAbortedError, TokenRefreshError
from backup.sync.models import SyncEvent, SyncSession


class SyncEngineTestCase(TestCase):
    """Base test case with common setup for SyncEngine tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / "test_secrets.json"
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

    def _make_drive_file(
        self,
        file_id: str = "file1",
        name: str = "test.txt",
        mime_type: str = "text/plain",
        size: int = 100,
        parents: list = None,
        trashed: bool = False,
        etag: str = "etag123",
    ) -> DriveFile:
        return DriveFile(
            id=file_id,
            name=name,
            mime_type=mime_type,
            size=size,
            modified_time=datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
            md5_checksum="abc123",
            parents=parents or ["root"],
            trashed=trashed,
            etag=etag,
        )

    def _make_change(
        self,
        file_id: str = "file1",
        removed: bool = False,
        file: DriveFile = None,
    ) -> DriveChange:
        return DriveChange(
            file_id=file_id,
            removed=removed,
            file=file,
            change_type="file",
            time=datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
        )


class InitialSyncTests(SyncEngineTestCase):
    """Tests for initial sync behavior."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_initial_sync_creates_session(self, mock_refresh, mock_build):
        """Initial sync should create a SyncSession with is_initial=True."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            # Mock get_start_page_token
            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            # Mock list_changes - return empty changes
            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "token123",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            # Check session was created
            session = SyncSession.objects.get(sync_root=self.sync_root)
            self.assertTrue(session.is_initial)
            self.assertEqual(session.status, "completed")

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_initial_sync_ignores_deletions(self, mock_refresh, mock_build):
        """Initial sync should ignore deletion events per ADR 0001."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            # Return a deletion change - should be filtered out
            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "deleted_file",
                        "removed": True,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                    }
                ],
                "newStartPageToken": "token123",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            # No items should be created for deletions
            self.assertEqual(BackupItem.objects.count(), 0)
            self.assertEqual(result.files_deleted, 0)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_initial_sync_saves_cursor(self, mock_refresh, mock_build):
        """Initial sync should save the cursor after completion."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "final_token"
            }

            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "final_token",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            self.sync_root.refresh_from_db()
            self.assertEqual(self.sync_root.sync_cursor, "final_token")
            self.assertIsNotNone(self.sync_root.last_sync_at)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    @patch.object(GoogleDriveClient, "download_file_to_stream")
    def test_initial_sync_downloads_files(self, mock_download, mock_refresh, mock_build):
        """Initial sync should download files and create BackupItems."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            # Return a file addition
            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "file1",
                            "name": "test.txt",
                            "mimeType": "text/plain",
                            "size": "100",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "etag123",
                        },
                    }
                ],
                "newStartPageToken": "token123",
            }

            # Mock file metadata for download
            mock_service.files().get().execute.return_value = {
                "id": "file1",
                "name": "test.txt",
                "mimeType": "text/plain",
                "size": "100",
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "parents": ["root"],
                "trashed": False,
                "etag": "etag123",
            }

            # Mock download
            def mock_download_impl(file_id, stream):
                stream.write(b"test content")
                return 12

            mock_download.side_effect = mock_download_impl

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            # Check file was created
            self.assertEqual(result.files_added, 1)
            item = BackupItem.objects.get(provider_item_id="file1")
            self.assertEqual(item.name, "test.txt")
            self.assertEqual(item.state, ItemState.ACTIVE)


class IncrementalSyncTests(SyncEngineTestCase):
    """Tests for incremental sync behavior."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_incremental_sync_uses_cursor(self, mock_refresh, mock_build):
        """Incremental sync should use the saved cursor."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Set a cursor to trigger incremental sync
            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "new_cursor",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            # Check session was incremental
            session = SyncSession.objects.get(sync_root=self.sync_root)
            self.assertFalse(session.is_initial)
            self.assertEqual(session.start_cursor, "saved_cursor")

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_incremental_sync_processes_deletions(self, mock_refresh, mock_build):
        """Incremental sync should process explicit deletions."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Create existing item
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
            )

            # Set cursor for incremental
            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            # Return deletion
            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": True,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                    }
                ],
                "newStartPageToken": "new_cursor",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            # Check item was marked deleted
            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.DELETED_UPSTREAM)
            self.assertEqual(result.files_deleted, 1)


class StateTransitionTests(SyncEngineTestCase):
    """Tests for deletion state machine transitions."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_missing_file_transitions_to_missing_upstream(self, mock_refresh, mock_build):
        """Files missing from sync should transition to MISSING_UPSTREAM."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Create existing item with old last_seen_at
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
                last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )

            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            # No changes (file is missing)
            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "new_cursor",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.MISSING_UPSTREAM)
            self.assertEqual(item.missing_since_sync_count, 1)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_missing_file_quarantined_after_threshold(self, mock_refresh, mock_build):
        """Files missing for 2 syncs should be quarantined."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Create item already missing once
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
                missing_since_sync_count=1,
                last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )

            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "new_cursor",
            }

            storage = AccountStorage(self.account)
            storage.ensure_directories()
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.QUARANTINED)
            self.assertEqual(item.missing_since_sync_count, 2)
            self.assertEqual(result.files_quarantined, 1)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    @patch.object(GoogleDriveClient, "download_file_to_stream")
    def test_reappearing_file_resets_state(self, mock_download, mock_refresh, mock_build):
        """Files that reappear should reset to ACTIVE state."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Create item in MISSING_UPSTREAM state
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                mime_type="text/plain",
                state=ItemState.MISSING_UPSTREAM,
                missing_since_sync_count=1,
                last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                etag="old_etag",
            )

            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            # File reappears in changes
            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "file1",
                            "name": "test.txt",
                            "mimeType": "text/plain",
                            "size": "100",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "etag123",
                        },
                    }
                ],
                "newStartPageToken": "new_cursor",
            }

            mock_service.files().get().execute.return_value = {
                "id": "file1",
                "name": "test.txt",
                "mimeType": "text/plain",
                "size": "100",
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "parents": ["root"],
                "trashed": False,
                "etag": "etag123",
            }

            def mock_download_impl(file_id, stream):
                stream.write(b"test content")
                return 12

            mock_download.side_effect = mock_download_impl

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            item.refresh_from_db()
            self.assertEqual(item.state, ItemState.ACTIVE)
            self.assertEqual(item.missing_since_sync_count, 0)


class ErrorHandlingTests(SyncEngineTestCase):
    """Tests for error handling in sync operations."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_disabled_account_raises_sync_aborted(self, mock_refresh, mock_build):
        """Sync should fail for disabled accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            self.account.is_active = False
            self.account.save()

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            with self.assertRaises(SyncAbortedError):
                engine.run_sync()

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_token_refresh_failure_raises_error(self, mock_refresh, mock_build):
        """Token refresh failure should raise TokenRefreshError."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_refresh.side_effect = Exception("Token expired")

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            with self.assertRaises(TokenRefreshError):
                engine.run_sync()

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    @patch.object(GoogleDriveClient, "download_file_to_stream")
    def test_download_error_logged_and_continues(self, mock_download, mock_refresh, mock_build):
        """Download errors should be logged but sync should continue."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            # Return two files
            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "file1",
                            "name": "test1.txt",
                            "mimeType": "text/plain",
                            "size": "100",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "etag1",
                        },
                    },
                    {
                        "fileId": "file2",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "file2",
                            "name": "test2.txt",
                            "mimeType": "text/plain",
                            "size": "100",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "etag2",
                        },
                    },
                ],
                "newStartPageToken": "token123",
            }

            def mock_get_metadata(fileId=None, **kwargs):
                mock_result = MagicMock()
                mock_result.execute.return_value = {
                    "id": fileId,
                    "name": f"{fileId}.txt",
                    "mimeType": "text/plain",
                    "size": "100",
                    "modifiedTime": "2024-01-15T10:30:00.000Z",
                    "parents": ["root"],
                    "trashed": False,
                    "etag": f"etag_{fileId}",
                }
                return mock_result

            mock_service.files().get.side_effect = mock_get_metadata

            # First file fails, second succeeds
            call_count = [0]

            def mock_download_impl(file_id, stream):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise FileNotDownloadableError("Cannot download")
                stream.write(b"test content")
                return 12

            mock_download.side_effect = mock_download_impl

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            # One file should have succeeded
            self.assertEqual(result.files_added, 1)
            self.assertEqual(len(result.errors), 1)

            # Error event should be logged
            error_events = SyncEvent.objects.filter(event_type="error")
            self.assertEqual(error_events.count(), 1)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_failed_session_marked_correctly(self, mock_refresh, mock_build):
        """Failed sync should mark session as failed."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.side_effect = Exception(
                "API Error"
            )

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            with self.assertRaises(Exception):
                engine.run_sync()

            session = SyncSession.objects.get(sync_root=self.sync_root)
            self.assertEqual(session.status, "failed")
            self.assertIn("API Error", session.error_message)


class FolderHandlingTests(SyncEngineTestCase):
    """Tests for folder handling during sync."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_folder_creates_backup_item(self, mock_refresh, mock_build):
        """Folders should create BackupItem records."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "folder1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "folder1",
                            "name": "Documents",
                            "mimeType": "application/vnd.google-apps.folder",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "folder_etag",
                        },
                    }
                ],
                "newStartPageToken": "token123",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            result = engine.run_sync()

            folder = BackupItem.objects.get(provider_item_id="folder1")
            self.assertEqual(folder.name, "Documents")
            self.assertEqual(folder.item_type, ItemType.FOLDER)
            self.assertEqual(folder.state, ItemState.ACTIVE)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_folder_creates_directory_on_filesystem(self, mock_refresh, mock_build):
        """Folders should create directories in the current/ tree."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "folder1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "folder1",
                            "name": "Documents",
                            "mimeType": "application/vnd.google-apps.folder",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "folder_etag",
                        },
                    }
                ],
                "newStartPageToken": "token123",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            folder_path = storage.current_dir / "Documents"
            self.assertTrue(folder_path.exists())
            self.assertTrue(folder_path.is_dir())


class BatchProcessingTests(SyncEngineTestCase):
    """Tests for batch processing behavior."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_cursor_updated_after_sync(self, mock_refresh, mock_build):
        """Sync cursor should be updated after sync completion."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            mock_service.changes().list().execute.return_value = {
                "changes": [],
                "newStartPageToken": "final_token",
            }

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            # Check cursor was set from start page token
            self.sync_root.refresh_from_db()
            self.assertEqual(self.sync_root.sync_cursor, "token123")


class FileVersionTests(SyncEngineTestCase):
    """Tests for FileVersion creation during sync."""

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    @patch.object(GoogleDriveClient, "download_file_to_stream")
    def test_file_version_created_on_download(self, mock_download, mock_refresh, mock_build):
        """FileVersion should be created when downloading a file."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": False,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                        "file": {
                            "id": "file1",
                            "name": "test.txt",
                            "mimeType": "text/plain",
                            "size": "100",
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "md5Checksum": "abc123",
                            "parents": ["root"],
                            "trashed": False,
                            "etag": "etag123",
                        },
                    }
                ],
                "newStartPageToken": "token123",
            }

            mock_service.files().get().execute.return_value = {
                "id": "file1",
                "name": "test.txt",
                "mimeType": "text/plain",
                "size": "100",
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "md5Checksum": "abc123",
                "parents": ["root"],
                "trashed": False,
                "etag": "etag123",
            }

            def mock_download_impl(file_id, stream):
                stream.write(b"test content")
                return 12

            mock_download.side_effect = mock_download_impl

            storage = AccountStorage(self.account)
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            # Check FileVersion was created
            item = BackupItem.objects.get(provider_item_id="file1")
            version = FileVersion.objects.get(backup_item=item)
            self.assertEqual(version.reason, VersionReason.UPDATE)
            self.assertIsNotNone(version.blob)

            # Check blob exists
            self.assertTrue(BackupBlob.objects.filter(pk=version.blob.digest).exists())

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_pre_delete_version_created_on_deletion(self, mock_refresh, mock_build):
        """PRE_DELETE FileVersion should be created when file is deleted."""
        with override_settings(SECRETS_FILE=self.secrets_file, BACKUP_ROOT=self.backup_root):
            secrets.set_tokens(
                self.account, access_token="test", refresh_token="test"
            )

            # Create existing item with a version
            blob = BackupBlob.objects.create(
                digest="sha256:" + "a" * 64,
                account=self.account,
                size_bytes=100,
            )
            item = BackupItem.objects.create(
                sync_root=self.sync_root,
                provider_item_id="file1",
                name="test.txt",
                path="test.txt",
                item_type=ItemType.FILE,
                state=ItemState.ACTIVE,
                provider_modified_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
                etag="etag123",
            )
            FileVersion.objects.create(
                account=self.account,
                backup_item=item,
                blob=blob,
                observed_path="test.txt",
                reason=VersionReason.UPDATE,
            )

            self.sync_root.sync_cursor = "saved_cursor"
            self.sync_root.last_sync_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
            self.sync_root.save()

            mock_service = MagicMock()
            mock_build.return_value = mock_service

            mock_service.changes().list().execute.return_value = {
                "changes": [
                    {
                        "fileId": "file1",
                        "removed": True,
                        "changeType": "file",
                        "time": "2024-01-15T10:30:00.000Z",
                    }
                ],
                "newStartPageToken": "new_cursor",
            }

            storage = AccountStorage(self.account)
            storage.ensure_directories()
            client = GoogleDriveClient(self.account)
            engine = SyncEngine(self.sync_root, storage, client)

            engine.run_sync()

            # Check PRE_DELETE version was created
            pre_delete = FileVersion.objects.filter(
                backup_item=item, reason=VersionReason.PRE_DELETE
            )
            self.assertEqual(pre_delete.count(), 1)
