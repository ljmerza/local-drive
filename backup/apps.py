import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class BackupConfig(AppConfig):
    name = 'backup'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        """
        Run when Django app is ready.

        Auto-discovers accounts from secrets.json on startup.
        """
        # Only run in the main process (not in migrations, etc.)
        import sys
        if 'runserver' not in sys.argv and 'migrate' not in sys.argv:
            return

        # Import here to avoid circular imports
        from backup.account_discovery import discover_accounts

        try:
            logger.info("Auto-discovering accounts from secrets file...")
            result = discover_accounts()

            if result.created_count > 0:
                logger.info(
                    f"Auto-discovery: Created {result.created_count} new account(s)"
                )
                for provider, email in result.created_accounts:
                    logger.info(f"  ✓ {provider} / {email}")

            if result.existing_accounts:
                logger.debug(
                    f"Auto-discovery: Found {len(result.existing_accounts)} existing account(s)"
                )

            if result.errors:
                logger.warning(
                    f"Auto-discovery: Encountered {len(result.errors)} error(s)"
                )
                for error in result.errors:
                    logger.warning(f"  ✗ {error}")

        except Exception as e:
            # Don't crash the app if discovery fails
            logger.error(f"Account auto-discovery failed: {e}", exc_info=True)
