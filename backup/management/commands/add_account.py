"""
Django management command to add a new account via OAuth.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from backup.models import Provider
from backup.providers.google_drive import get_authorization_url


class Command(BaseCommand):
    help = "Add a new account by initiating OAuth flow"

    def add_arguments(self, parser):
        parser.add_argument(
            "provider",
            choices=["google", "onedrive"],
            help="Cloud provider (google, onedrive)",
        )

    def handle(self, *args, **options):
        provider = options["provider"]

        if provider == "google":
            self._add_google_account()
        elif provider == "onedrive":
            self._add_onedrive_account()

    def _add_google_account(self):
        """Initiate Google OAuth flow."""
        # Check if OAuth is configured
        if not getattr(settings, "GOOGLE_CLIENT_ID", None):
            self.stderr.write(self.style.ERROR(
                "Google OAuth not configured.\n\n"
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in settings,\n"
                "or add them to secrets.json under oauth_clients.google"
            ))
            return

        auth_url, state = get_authorization_url()

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("Google Drive OAuth"))
        self.stdout.write("=" * 60)
        self.stdout.write("\n1. Open this URL in your browser:\n")
        self.stdout.write(self.style.WARNING(auth_url))
        self.stdout.write("\n2. Sign in and authorize the application")
        self.stdout.write("\n3. You'll be redirected to the callback URL")
        self.stdout.write("\n4. The account will be created automatically\n")
        self.stdout.write("=" * 60)

        # Show the expected callback URL
        redirect_uri = getattr(settings, "GOOGLE_REDIRECT_URI", "not configured")
        self.stdout.write(f"\nCallback URL: {redirect_uri}")
        self.stdout.write("Make sure the Django server is running to handle the callback.\n")

    def _add_onedrive_account(self):
        """Placeholder for OneDrive OAuth flow."""
        self.stderr.write(self.style.ERROR(
            "OneDrive provider not yet implemented.\n\n"
            "See ADR 0003 for implementation roadmap."
        ))
