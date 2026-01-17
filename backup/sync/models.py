"""
Models for tracking sync operations and events.
"""

from django.db import models

from backup.models import BackupItem, SyncRoot


class SyncSession(models.Model):
    """
    Records each sync operation for audit and debugging.

    Tracks the complete lifecycle of a sync run including statistics
    on files processed, bytes downloaded, and any errors encountered.
    """

    sync_root = models.ForeignKey(
        SyncRoot, on_delete=models.CASCADE, related_name="sessions"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Sync type
    is_initial = models.BooleanField(default=False)

    # Cursors
    start_cursor = models.TextField(blank=True)
    end_cursor = models.TextField(blank=True)

    # Status
    status = models.CharField(
        max_length=20,
        choices=[
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("partial", "Partial Success"),
        ],
        default="running",
    )

    # Statistics
    files_added = models.PositiveIntegerField(default=0)
    files_updated = models.PositiveIntegerField(default=0)
    files_deleted = models.PositiveIntegerField(default=0)
    files_quarantined = models.PositiveIntegerField(default=0)
    bytes_downloaded = models.BigIntegerField(default=0)

    # Error tracking
    error_message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["sync_root", "-started_at"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-started_at"]

    def __str__(self):
        status_display = self.get_status_display()
        sync_type = "Initial" if self.is_initial else "Incremental"
        return f"{sync_type} sync of {self.sync_root.name} - {status_display}"


class SyncEvent(models.Model):
    """
    Individual events during a sync session.

    Provides a detailed audit trail of every action taken during sync,
    including file additions, updates, deletions, and errors.
    """

    session = models.ForeignKey(
        SyncSession, on_delete=models.CASCADE, related_name="events"
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    event_type = models.CharField(
        max_length=20,
        choices=[
            ("file_added", "File Added"),
            ("file_updated", "File Updated"),
            ("file_deleted", "File Deleted"),
            ("file_quarantined", "File Quarantined"),
            ("error", "Error"),
            ("checkpoint", "Checkpoint"),
        ],
    )

    backup_item = models.ForeignKey(
        BackupItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sync_events",
    )

    # Event details
    provider_file_id = models.CharField(max_length=255, blank=True)
    file_path = models.TextField(blank=True)
    message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["session", "timestamp"]),
            models.Index(fields=["event_type"]),
        ]
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.get_event_type_display()}: {self.file_path or self.provider_file_id or 'N/A'}"
