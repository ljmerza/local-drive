"""
Django management command to sync a cloud storage account.
"""

from django.core.management.base import BaseCommand, CommandError

from backup.models import Account, SyncRoot
from backup.providers.google_drive import GoogleDriveClient
from backup.storage import AccountStorage
from backup.sync import SyncEngine


class Command(BaseCommand):
    help = "Sync a cloud storage account to local backup"

    def add_arguments(self, parser):
        parser.add_argument(
            "account_id",
            type=int,
            help="Account ID to sync",
        )
        parser.add_argument(
            "--sync-root-id",
            type=int,
            help="Specific sync root ID (default: all enabled roots)",
        )
        parser.add_argument(
            "--force-initial",
            action="store_true",
            help="Force initial sync even if cursor exists",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of changes to process per batch (default: 100)",
        )

    def handle(self, *args, **options):
        account_id = options["account_id"]

        # Get account
        try:
            account = Account.objects.get(id=account_id, is_active=True)
        except Account.DoesNotExist:
            raise CommandError(f"Account {account_id} not found or inactive")

        self.stdout.write(f"Syncing account: {account.name} ({account.get_provider_display()})")

        # Get sync roots
        if options["sync_root_id"]:
            try:
                sync_roots = [
                    SyncRoot.objects.get(
                        id=options["sync_root_id"],
                        account=account,
                        is_enabled=True,
                    )
                ]
            except SyncRoot.DoesNotExist:
                raise CommandError(
                    f"Sync root {options['sync_root_id']} not found or not enabled"
                )
        else:
            sync_roots = list(account.sync_roots.filter(is_enabled=True))

        if not sync_roots:
            raise CommandError("No enabled sync roots found")

        # Create client and storage
        client = GoogleDriveClient(account)
        storage = AccountStorage(account)

        # Sync each root
        for sync_root in sync_roots:
            self.stdout.write(self.style.WARNING(f"\nSyncing: {sync_root.name}"))

            # Force initial if requested
            if options["force_initial"]:
                self.stdout.write("Forcing initial sync (resetting cursor)")
                sync_root.sync_cursor = ""
                sync_root.save()

            # Create engine
            engine = SyncEngine(
                sync_root=sync_root,
                storage=storage,
                client=client,
                batch_size=options["batch_size"],
            )

            # Run sync
            try:
                result = engine.run_sync()

                # Display results
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n✓ Sync completed successfully:\n"
                        f"  - Files added: {result.files_added}\n"
                        f"  - Files updated: {result.files_updated}\n"
                        f"  - Files deleted: {result.files_deleted}\n"
                        f"  - Files quarantined: {result.files_quarantined}\n"
                        f"  - Bytes downloaded: {result.bytes_downloaded:,}\n"
                        f"  - Errors: {len(result.errors)}"
                    )
                )

                if result.errors:
                    self.stdout.write(
                        self.style.WARNING(
                            f"\n⚠ Encountered {len(result.errors)} error(s) during sync"
                        )
                    )
                    for i, error in enumerate(result.errors[:5], 1):
                        self.stdout.write(f"  {i}. {error}")
                    if len(result.errors) > 5:
                        self.stdout.write(f"  ... and {len(result.errors) - 5} more")

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"\n✗ Sync failed: {e}"))
                raise CommandError(f"Sync failed: {e}")

        self.stdout.write(self.style.SUCCESS("\n✓ All syncs completed"))
