"""Tests for dashboard view."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from backup import secrets
from backup.models import Account, Provider, SyncRoot


class DashboardViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.json"
        self.secrets_file.write_text("{}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_empty(self):
        """Test dashboard with no accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "No accounts configured")
            self.assertContains(response, "Add Google Account")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_with_accounts(self):
        """Test dashboard with accounts."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "test@example.com")
            self.assertContains(response, "Test User")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_token_status_missing(self):
        """Test dashboard shows missing token status."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Missing")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_token_status_valid(self):
        """Test dashboard shows valid token status."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            # Set tokens with future expiry
            secrets.set_tokens(
                account,
                access_token="test_access",
                refresh_token="test_refresh",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Valid")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_token_status_expiring(self):
        """Test dashboard shows expiring token status."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            # Set tokens expiring in 30 minutes
            secrets.set_tokens(
                account,
                access_token="test_access",
                refresh_token="test_refresh",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Expiring")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_token_status_expired(self):
        """Test dashboard shows expired token status."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            # Set tokens that expired
            secrets.set_tokens(
                account,
                access_token="test_access",
                refresh_token="test_refresh",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Expired")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_with_sync_root(self):
        """Test dashboard shows last sync time."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            account = Account.objects.create(
                provider=Provider.GOOGLE_DRIVE,
                name="Test User",
                email="test@example.com",
                is_active=True,
            )
            SyncRoot.objects.create(
                account=account,
                provider_root_id="root",
                name="My Drive",
                last_sync_at=datetime.now(timezone.utc),
                is_enabled=True,
            )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            # Should not show "Never" for last sync
            self.assertNotContains(response, ">Never<")

    @override_settings(SECRETS_FILE=None)
    def test_dashboard_stats(self):
        """Test dashboard shows summary stats."""
        with override_settings(SECRETS_FILE=self.secrets_file):
            # Create accounts with different token states
            for i in range(3):
                Account.objects.create(
                    provider=Provider.GOOGLE_DRIVE,
                    name=f"User {i}",
                    email=f"user{i}@example.com",
                    is_active=True,
                )

            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            # Check stats are present
            self.assertContains(response, "Total Accounts")
            self.assertContains(response, "Healthy")

    @override_settings(SECRETS_FILE=None)
    @patch("backup.views.dashboard._test_connection")
    def test_dashboard_connection_test(self, mock_test):
        """Test dashboard connection testing."""
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
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )

            mock_test.return_value = ("connected", None)

            response = self.client.get("/?test=1")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Connection")
            mock_test.assert_called()
