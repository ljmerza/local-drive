"""
Django management command to list all accounts and their status.
"""

from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from backup import secrets
from backup.models import Account


class Command(BaseCommand):
    help = "List all accounts with their sync status"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output as JSON",
        )

    def handle(self, *args, **options):
        accounts = Account.objects.filter(is_active=True).select_related().order_by("provider", "email")

        if not accounts.exists():
            self.stdout.write(self.style.WARNING("No accounts found."))
            self.stdout.write("\nRun 'python manage.py discover_accounts' to import from secrets.json")
            return

        if options["json"]:
            self._output_json(accounts)
        else:
            self._output_table(accounts)

    def _get_token_status(self, account: Account) -> tuple[str, str]:
        """Get token status and expiry info."""
        tokens = secrets.get_tokens(account)
        if not tokens:
            return "missing", ""

        expires_at = tokens.get("expires_at")
        if not expires_at:
            return "unknown", ""

        now = datetime.now(timezone.utc)
        if expires_at < now:
            return "expired", expires_at.strftime("%Y-%m-%d %H:%M")

        delta = expires_at - now
        if delta.total_seconds() < 3600:
            return "expiring", f"{int(delta.total_seconds() / 60)}m"

        return "valid", expires_at.strftime("%Y-%m-%d %H:%M")

    def _get_last_sync(self, account: Account) -> str:
        """Get last sync time from any sync root."""
        sync_roots = account.sync_roots.filter(is_enabled=True).order_by("-last_sync_at")
        if not sync_roots.exists():
            return "never"

        last_sync = sync_roots.first().last_sync_at
        if not last_sync:
            return "never"

        return last_sync.strftime("%Y-%m-%d %H:%M")

    def _output_table(self, accounts):
        """Output accounts as formatted table."""
        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(f"{'ID':<4} {'Provider':<12} {'Email':<30} {'Tokens':<10} {'Last Sync':<16}")
        self.stdout.write("=" * 80)

        for account in accounts:
            token_status, _ = self._get_token_status(account)
            last_sync = self._get_last_sync(account)

            # Style token status
            if token_status == "valid":
                status_display = self.style.SUCCESS("valid")
            elif token_status == "expiring":
                status_display = self.style.WARNING("expiring")
            elif token_status == "expired":
                status_display = self.style.ERROR("expired")
            else:
                status_display = self.style.ERROR(token_status)

            self.stdout.write(
                f"{account.id:<4} {account.provider:<12} {account.email:<30} "
                f"{status_display:<19} {last_sync:<16}"
            )

        self.stdout.write("=" * 80)
        self.stdout.write(f"Total: {accounts.count()} account(s)\n")

    def _output_json(self, accounts):
        """Output accounts as JSON."""
        import json

        data = []
        for account in accounts:
            token_status, expires_at = self._get_token_status(account)
            data.append({
                "id": account.id,
                "provider": account.provider,
                "email": account.email,
                "name": account.name,
                "token_status": token_status,
                "token_expires_at": expires_at,
                "last_sync": self._get_last_sync(account),
                "is_active": account.is_active,
            })

        self.stdout.write(json.dumps(data, indent=2))
