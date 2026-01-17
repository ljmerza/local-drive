"""
Django management command to run garbage collection.
"""

from django.core.management.base import BaseCommand, CommandError

from backup.gc import GarbageCollector
from backup.models import Account


class Command(BaseCommand):
    help = "Run garbage collection to clean up old versions and orphaned blobs"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without actually deleting",
        )
        parser.add_argument(
            "--account-id",
            type=int,
            help="Specific account ID to run GC for (default: all accounts)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of items to process per batch (default: 100)",
        )

    def handle(self, *args, **options):
        account = None

        if options["account_id"]:
            try:
                account = Account.objects.get(id=options["account_id"])
            except Account.DoesNotExist:
                raise CommandError(f"Account {options['account_id']} not found")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Running in dry-run mode"))

        gc = GarbageCollector(
            account=account,
            dry_run=options["dry_run"],
            batch_size=options["batch_size"],
        )

        result = gc.run()

        # Display results
        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n[DRY RUN] Would have:\n"
                    f"  - Purged {result.versions_purged} old versions\n"
                    f"  - Deleted {result.blobs_deleted} orphaned blobs\n"
                    f"  - Purged {result.quarantine_purged} quarantined items\n"
                    f"  - Freed {result.bytes_freed:,} bytes"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nGarbage collection completed:\n"
                    f"  - Purged {result.versions_purged} old versions\n"
                    f"  - Deleted {result.blobs_deleted} orphaned blobs\n"
                    f"  - Purged {result.quarantine_purged} quarantined items\n"
                    f"  - Freed {result.bytes_freed:,} bytes"
                )
            )

        if result.errors:
            self.stdout.write(
                self.style.WARNING(f"\nEncountered {len(result.errors)} error(s):")
            )
            for error in result.errors[:10]:
                self.stdout.write(f"  - {error}")
            if len(result.errors) > 10:
                self.stdout.write(f"  ... and {len(result.errors) - 10} more")
