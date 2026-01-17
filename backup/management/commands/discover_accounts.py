"""
Django management command to discover accounts from secrets file.
"""

from django.core.management.base import BaseCommand

from backup.account_discovery import discover_accounts


class Command(BaseCommand):
    help = "Discover and create accounts from secrets.json"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed output",
        )

    def handle(self, *args, **options):
        verbose = options["verbose"]

        self.stdout.write("Scanning secrets.json for accounts...")

        result = discover_accounts()

        # Show results
        if result.total_found == 0:
            self.stdout.write(
                self.style.WARNING(
                    "\nNo accounts found in secrets.json\n\n"
                    "Add your OAuth tokens to secrets.json first.\n"
                    "See OAUTH_PLAYGROUND_GUIDE.md for instructions."
                )
            )
            return

        self.stdout.write(f"\nFound {result.total_found} account(s) in secrets.json\n")

        # Show created accounts
        if result.created_accounts:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✓ Created {result.created_count} new account(s):"
                )
            )
            for provider, email in result.created_accounts:
                self.stdout.write(f"  • {provider} / {email}")

        # Show existing accounts
        if result.existing_accounts:
            msg = f"\n→ Found {len(result.existing_accounts)} existing account(s)"
            if verbose:
                self.stdout.write(msg + ":")
                for provider, email in result.existing_accounts:
                    self.stdout.write(f"  • {provider} / {email}")
            else:
                self.stdout.write(msg + " (already in database)")

        # Show errors
        if result.errors:
            self.stdout.write(
                self.style.WARNING(
                    f"\n⚠ Encountered {len(result.errors)} error(s):"
                )
            )
            for error in result.errors:
                self.stdout.write(f"  • {error}")

        # Show next steps
        if result.created_accounts:
            from backup.models import Account

            accounts = Account.objects.filter(is_active=True).order_by('id')

            self.stdout.write(
                self.style.SUCCESS(
                    "\n✓ Accounts are ready to sync!\n\n"
                    "Run sync with:\n"
                )
            )

            for account in accounts:
                self.stdout.write(
                    f"  python manage.py sync_account {account.id}  "
                    f"# {account.email}"
                )
