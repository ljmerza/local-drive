"""Tests for management commands."""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from backup import secrets
from backup.models import Account, Provider, SyncRoot


class ListAccountsCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.json"
        self.secrets_file.write_text("{}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @override_settings(SECRETS_FILE=None)
    def test_list_accounts_empty(self):
        """Test list_accounts with no accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            out = StringIO()
            call_command("list_accounts", stdout=out)
            self.assertIn("No accounts found", out.getvalue())

    @override_settings(SECRETS_FILE=None)
    def test_list_accounts_with_accounts(self):
        """Test list_accounts with accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            secrets.set_tokens(
                account,
                access_token="test_access",
                refresh_token="test_refresh",
            )

            out = StringIO()
            call_command("list_accounts", stdout=out)
            output = out.getvalue()

            self.assertIn("test@example.com", output)
            self.assertIn("google_drive", output)

    @override_settings(SECRETS_FILE=None)
    def test_list_accounts_json_output(self):
        """Test list_accounts JSON output."""
        import json

        with override_settings(SECRETS_FILE=self.secrets_file):
            Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )

            out = StringIO()
            call_command("list_accounts", "--json", stdout=out)
            data = json.loads(out.getvalue())

            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["email"], "test@example.com")


class VerifyTokensCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.json"
        self.secrets_file.write_text("{}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @override_settings(SECRETS_FILE=None)
    def test_verify_tokens_no_accounts(self):
        """Test verify_tokens with no accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            out = StringIO()
            call_command("verify_tokens", stdout=out)
            self.assertIn("No active accounts found", out.getvalue())

    @override_settings(SECRETS_FILE=None)
    def test_verify_tokens_no_tokens(self):
        """Test verify_tokens with account but no tokens."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )

            out = StringIO()
            call_command("verify_tokens", stdout=out)
            self.assertIn("NO TOKENS", out.getvalue())

    @override_settings(SECRETS_FILE=None)
    @patch("backup.management.commands.verify_tokens.GoogleDriveClient")
    def test_verify_tokens_valid(self, mock_client_class):
        """Test verify_tokens with valid tokens."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            secrets.set_tokens(
                account,
                access_token="test_access",
                refresh_token="test_refresh",
            )

            # Mock the client
            mock_client = MagicMock()
            mock_client.get_user_info.return_value = {"email": "test@example.com"}
            mock_client_class.return_value = mock_client

            out = StringIO()
            call_command("verify_tokens", stdout=out)
            self.assertIn("VALID", out.getvalue())

    @override_settings(SECRETS_FILE=None)
    def test_verify_tokens_specific_account(self):
        """Test verify_tokens for specific account ID."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )

            out = StringIO()
            err = StringIO()
            call_command("verify_tokens", account.id, stdout=out, stderr=err)
            # Should find the account (even without tokens)
            self.assertIn("NO TOKENS", out.getvalue())

    @override_settings(SECRETS_FILE=None)
    def test_verify_tokens_invalid_account_id(self):
        """Test verify_tokens with invalid account ID."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            out = StringIO()
            err = StringIO()
            call_command("verify_tokens", 99999, stdout=out, stderr=err)
            self.assertIn("not found", err.getvalue())


class AddAccountCommandTests(TestCase):
    @patch("backup.management.commands.add_account.get_authorization_url")
    def test_add_google_account(self, mock_get_url):
        """Test add_account for Google."""
        mock_get_url.return_value = ("https://accounts.google.com/oauth", "state123")

        out = StringIO()
        with override_settings(GOOGLE_CLIENT_ID="test_id"):
            call_command("add_account", "google", stdout=out)

        output = out.getvalue()
        self.assertIn("Google Drive OAuth", output)
        self.assertIn("https://accounts.google.com/oauth", output)

    def test_add_onedrive_not_implemented(self):
        """Test add_account for OneDrive shows not implemented."""
        out = StringIO()
        err = StringIO()
        call_command("add_account", "onedrive", stdout=out, stderr=err)
        self.assertIn("not yet implemented", err.getvalue())
