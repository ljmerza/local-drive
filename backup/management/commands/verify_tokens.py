"""
Django management command to verify token validity for accounts.
"""

from django.core.management.base import BaseCommand

from backup import secrets
from backup.models import Account, Provider
from backup.providers.google_drive import GoogleDriveClient, TokenExpiredError


class Command(BaseCommand):
    help = "Verify OAuth token validity by testing API connection"

    def add_arguments(self, parser):
        parser.add_argument(
            "account_id",
            nargs="?",
            type=int,
            help="Account ID to verify (optional, verifies all if not specified)",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Attempt to refresh expired tokens",
        )

    def handle(self, *args, **options):
        account_id = options.get("account_id")
        do_refresh = options["refresh"]

        if account_id:
            try:
                accounts = [Account.objects.get(id=account_id, is_active=True)]
            except Account.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"Account {account_id} not found or inactive"))
                return
        else:
            accounts = Account.objects.filter(is_active=True).order_by("id")

        if not accounts:
            self.stdout.write(self.style.WARNING("No active accounts found."))
            return

        self.stdout.write(f"\nVerifying {len(accounts)} account(s)...\n")

        results = {"valid": 0, "refreshed": 0, "failed": 0, "no_tokens": 0}

        for account in accounts:
            self._verify_account(account, do_refresh, results)

        # Summary
        self.stdout.write("\n" + "-" * 40)
        self.stdout.write(
            f"Valid: {results['valid']}  "
            f"Refreshed: {results['refreshed']}  "
            f"Failed: {results['failed']}  "
            f"No tokens: {results['no_tokens']}"
        )

    def _verify_account(self, account: Account, do_refresh: bool, results: dict):
        """Verify a single account's tokens."""
        prefix = f"[{account.id}] {account.email}"

        # Check if tokens exist
        if not secrets.has_tokens(account):
            self.stdout.write(f"{prefix}: " + self.style.ERROR("NO TOKENS"))
            results["no_tokens"] += 1
            return

        # Verify by provider
        if account.provider == Provider.GOOGLE_DRIVE:
            self._verify_google_drive(account, prefix, do_refresh, results)
        else:
            self.stdout.write(f"{prefix}: " + self.style.WARNING(f"Unsupported provider: {account.provider}"))

    def _verify_google_drive(self, account: Account, prefix: str, do_refresh: bool, results: dict):
        """Verify Google Drive account tokens."""
        client = GoogleDriveClient(account)

        try:
            if do_refresh:
                refreshed = client.refresh_token_if_needed()
                if refreshed:
                    self.stdout.write(f"{prefix}: " + self.style.SUCCESS("REFRESHED"))
                    results["refreshed"] += 1
                    return

            # Try to get user info to verify token works
            user_info = client.get_user_info()
            email = user_info.get("email", "unknown")
            self.stdout.write(f"{prefix}: " + self.style.SUCCESS(f"VALID (verified as {email})"))
            results["valid"] += 1

        except TokenExpiredError as e:
            self.stdout.write(f"{prefix}: " + self.style.ERROR(f"EXPIRED - {e}"))
            results["failed"] += 1

        except Exception as e:
            self.stdout.write(f"{prefix}: " + self.style.ERROR(f"ERROR - {e}"))
            results["failed"] += 1
