"""Tests for Google Drive provider."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from backup import secrets
from backup.models import Account, Provider
from backup.providers.google_drive import (
    GOOGLE_DOC_TYPES,
    ChangesPage,
    DriveChange,
    DriveFile,
    FileNotDownloadableError,
    GoogleDriveClient,
)


class DriveFileTests(TestCase):
    def test_from_api_response_regular_file(self):
        data = {
            "id": "file123",
            "name": "document.pdf",
            "mimeType": "application/pdf",
            "size": "12345",
            "modifiedTime": "2024-01-15T10:30:00.000Z",
            "md5Checksum": "abc123",
            "parents": ["folder456"],
            "trashed": False,
        }

        file = DriveFile.from_api_response(data)

        self.assertEqual(file.id, "file123")
        self.assertEqual(file.name, "document.pdf")
        self.assertEqual(file.mime_type, "application/pdf")
        self.assertEqual(file.size, 12345)
        self.assertEqual(file.md5_checksum, "abc123")
        self.assertEqual(file.parents, ["folder456"])
        self.assertFalse(file.trashed)
        self.assertFalse(file.is_folder)
        self.assertFalse(file.is_google_doc)
        self.assertTrue(file.is_downloadable)

    def test_from_api_response_google_doc(self):
        data = {
            "id": "doc123",
            "name": "My Document",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2024-01-15T10:30:00.000Z",
            "parents": ["folder456"],
            "trashed": False,
        }

        file = DriveFile.from_api_response(data)

        self.assertTrue(file.is_google_doc)
        self.assertTrue(file.is_downloadable)
        self.assertIsNone(file.size)
        self.assertIsNone(file.md5_checksum)
        self.assertEqual(
            file.export_mime_type,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(file.export_extension, ".docx")

    def test_from_api_response_folder(self):
        data = {
            "id": "folder123",
            "name": "My Folder",
            "mimeType": "application/vnd.google-apps.folder",
            "modifiedTime": "2024-01-15T10:30:00.000Z",
            "parents": ["root"],
            "trashed": False,
        }

        file = DriveFile.from_api_response(data)

        self.assertTrue(file.is_folder)
        self.assertFalse(file.is_downloadable)

    def test_from_api_response_shortcut(self):
        data = {
            "id": "shortcut123",
            "name": "Shortcut",
            "mimeType": "application/vnd.google-apps.shortcut",
            "modifiedTime": "2024-01-15T10:30:00.000Z",
            "parents": ["folder456"],
            "trashed": False,
        }

        file = DriveFile.from_api_response(data)

        self.assertFalse(file.is_downloadable)

    def test_all_google_doc_types_have_export_info(self):
        for mime_type, (export_mime, extension) in GOOGLE_DOC_TYPES.items():
            data = {
                "id": "test",
                "name": "test",
                "mimeType": mime_type,
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "parents": [],
                "trashed": False,
            }
            file = DriveFile.from_api_response(data)

            self.assertTrue(file.is_google_doc, f"{mime_type} should be a Google Doc")
            self.assertEqual(file.export_mime_type, export_mime)
            self.assertEqual(file.export_extension, extension)


class DriveChangeTests(TestCase):
    def test_from_api_response_file_change(self):
        data = {
            "fileId": "file123",
            "removed": False,
            "changeType": "file",
            "time": "2024-01-15T10:30:00.000Z",
            "file": {
                "id": "file123",
                "name": "document.pdf",
                "mimeType": "application/pdf",
                "size": "12345",
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "parents": ["folder456"],
                "trashed": False,
            },
        }

        change = DriveChange.from_api_response(data)

        self.assertEqual(change.file_id, "file123")
        self.assertFalse(change.removed)
        self.assertEqual(change.change_type, "file")
        self.assertIsNotNone(change.file)
        self.assertEqual(change.file.name, "document.pdf")

    def test_from_api_response_removal(self):
        data = {
            "fileId": "file123",
            "removed": True,
            "changeType": "file",
            "time": "2024-01-15T10:30:00.000Z",
        }

        change = DriveChange.from_api_response(data)

        self.assertEqual(change.file_id, "file123")
        self.assertTrue(change.removed)
        self.assertIsNone(change.file)


class ChangesPageTests(TestCase):
    def test_has_more_with_next_page_token(self):
        page = ChangesPage(
            changes=[],
            new_start_page_token=None,
            next_page_token="next123",
        )
        self.assertTrue(page.has_more)

    def test_has_more_with_new_start_token(self):
        page = ChangesPage(
            changes=[],
            new_start_page_token="new123",
            next_page_token=None,
        )
        self.assertFalse(page.has_more)


class GoogleDriveClientTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / "test_secrets.json"
        self.account = Account.objects.create(
            provider=Provider.GOOGLE_DRIVE,
            name="Test Account",
            email="test@example.com",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            client = GoogleDriveClient(self.account)
            self.assertEqual(client.account, self.account)
            self.assertIsNone(client._credentials)
            self.assertIsNone(client._service)

    def test_get_credentials(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Store tokens in secrets file
            secrets.set_tokens(
                self.account,
                access_token="test_access_token",
                refresh_token="test_refresh_token",
                expires_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )

            client = GoogleDriveClient(self.account)
            creds = client._get_credentials()

            self.assertIsNotNone(creds)
            self.assertEqual(creds.token, "test_access_token")

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_get_about(self, mock_refresh, mock_build):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_token",
                refresh_token="test_refresh",
            )
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.about().get().execute.return_value = {
                "user": {
                    "emailAddress": "test@example.com",
                    "displayName": "Test User",
                },
                "storageQuota": {
                    "limit": "15000000000",
                    "usage": "5000000000",
                },
            }

            client = GoogleDriveClient(self.account)
            about = client.get_about()

            self.assertEqual(about["email"], "test@example.com")
            self.assertEqual(about["display_name"], "Test User")
            self.assertIn("storage_quota", about)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_get_start_page_token(self, mock_refresh, mock_build):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_token",
                refresh_token="test_refresh",
            )
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.changes().getStartPageToken().execute.return_value = {
                "startPageToken": "token123"
            }

            client = GoogleDriveClient(self.account)
            token = client.get_start_page_token()

            self.assertEqual(token, "token123")

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_list_changes(self, mock_refresh, mock_build):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_token",
                refresh_token="test_refresh",
            )
            mock_service = MagicMock()
            mock_build.return_value = mock_service
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
                            "modifiedTime": "2024-01-15T10:30:00.000Z",
                            "parents": ["root"],
                            "trashed": False,
                        },
                    }
                ],
                "newStartPageToken": "newtoken123",
            }

            client = GoogleDriveClient(self.account)
            page = client.list_changes("oldtoken")

            self.assertEqual(len(page.changes), 1)
            self.assertEqual(page.changes[0].file_id, "file1")
            self.assertEqual(page.new_start_page_token, "newtoken123")
            self.assertFalse(page.has_more)

    @patch("backup.providers.google_drive.build")
    @patch.object(GoogleDriveClient, "refresh_token_if_needed")
    def test_get_file_metadata(self, mock_refresh, mock_build):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_token",
                refresh_token="test_refresh",
            )
            mock_service = MagicMock()
            mock_build.return_value = mock_service
            mock_service.files().get().execute.return_value = {
                "id": "file123",
                "name": "document.pdf",
                "mimeType": "application/pdf",
                "size": "12345",
                "modifiedTime": "2024-01-15T10:30:00.000Z",
                "parents": ["folder456"],
                "trashed": False,
            }

            client = GoogleDriveClient(self.account)
            file = client.get_file_metadata("file123")

            self.assertEqual(file.id, "file123")
            self.assertEqual(file.name, "document.pdf")
            self.assertEqual(file.size, 12345)
