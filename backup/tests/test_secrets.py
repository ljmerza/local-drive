"""Tests for secrets management."""

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from backup.models import Account, Provider
from backup import secrets


class SecretsManagerTests(TestCase):
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

    @override_settings(SECRETS_FILE=None)
    def test_get_account_key(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            key = secrets._get_account_key(self.account)
            self.assertEqual(key, "google_drive:test@example.com")

    def test_set_and_get_tokens(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            expires = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

            secrets.set_tokens(
                self.account,
                access_token="test_access",
                refresh_token="test_refresh",
                expires_at=expires,
            )

            tokens = secrets.get_tokens(self.account)

            self.assertIsNotNone(tokens)
            self.assertEqual(tokens["access_token"], "test_access")
            self.assertEqual(tokens["refresh_token"], "test_refresh")
            self.assertEqual(tokens["expires_at"], expires)

    def test_get_tokens_nonexistent_account(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            other_account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Other Account",
                email="other@example.com",
            )

            tokens = secrets.get_tokens(other_account)
            self.assertIsNone(tokens)

    def test_delete_tokens(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_access",
                refresh_token="test_refresh",
            )

            self.assertTrue(secrets.has_tokens(self.account))

            result = secrets.delete_tokens(self.account)

            self.assertTrue(result)
            self.assertFalse(secrets.has_tokens(self.account))

    def test_delete_tokens_nonexistent(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            result = secrets.delete_tokens(self.account)
            self.assertFalse(result)

    def test_has_tokens(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            self.assertFalse(secrets.has_tokens(self.account))

            secrets.set_tokens(
                self.account,
                access_token="test_access",
                refresh_token="test_refresh",
            )

            self.assertTrue(secrets.has_tokens(self.account))

    def test_list_accounts(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Initially empty
            self.assertEqual(secrets.list_accounts(), [])

            # Add some accounts
            secrets.set_tokens(
                self.account,
                access_token="test1",
                refresh_token="refresh1",
            )

            other_account = Account.objects.create(
                provider=Provider.ONEDRIVE,
                name="OneDrive Account",
                email="onedrive@example.com",
            )
            secrets.set_tokens(
                other_account,
                access_token="test2",
                refresh_token="refresh2",
            )

            accounts = secrets.list_accounts()
            self.assertEqual(len(accounts), 2)
            self.assertIn("google_drive:test@example.com", accounts)
            self.assertIn("onedrive:onedrive@example.com", accounts)

    def test_file_permissions(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_access",
                refresh_token="test_refresh",
            )

            # Check file permissions are 600
            file_stat = os.stat(self.secrets_file)
            mode = stat.S_IMODE(file_stat.st_mode)
            self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_atomic_write(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Write initial data
            secrets.set_tokens(
                self.account,
                access_token="initial",
                refresh_token="initial",
            )

            # Verify no temp files left behind
            temp_files = list(Path(self.temp_dir).glob(".secrets_*.tmp"))
            self.assertEqual(len(temp_files), 0)

    def test_update_existing_tokens(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Initial tokens
            secrets.set_tokens(
                self.account,
                access_token="old_access",
                refresh_token="old_refresh",
            )

            # Update tokens
            secrets.set_tokens(
                self.account,
                access_token="new_access",
                refresh_token="new_refresh",
            )

            tokens = secrets.get_tokens(self.account)
            self.assertEqual(tokens["access_token"], "new_access")
            self.assertEqual(tokens["refresh_token"], "new_refresh")

    def test_multiple_accounts(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            account2 = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Second Account",
                email="second@example.com",
            )

            secrets.set_tokens(
                self.account,
                access_token="token1",
                refresh_token="refresh1",
            )
            secrets.set_tokens(
                account2,
                access_token="token2",
                refresh_token="refresh2",
            )

            tokens1 = secrets.get_tokens(self.account)
            tokens2 = secrets.get_tokens(account2)

            self.assertEqual(tokens1["access_token"], "token1")
            self.assertEqual(tokens2["access_token"], "token2")

    def test_expires_at_none(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            secrets.set_tokens(
                self.account,
                access_token="test_access",
                refresh_token="test_refresh",
                expires_at=None,
            )

            tokens = secrets.get_tokens(self.account)
            self.assertIsNone(tokens["expires_at"])

    def test_load_empty_file(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # File doesn't exist yet
            data = secrets._load_secrets()
            self.assertEqual(data, {})

    def test_invalid_json_file(self):
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Write invalid JSON
            self.secrets_file.write_text("not valid json{{{")

            with self.assertRaises(secrets.SecretsFileError):
                secrets._load_secrets()
