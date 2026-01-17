from django.db import models


class Provider(models.TextChoices):
    GOOGLE_DRIVE = "google_drive", "Google Drive"
    ONEDRIVE = "onedrive", "OneDrive"


class ItemType(models.TextChoices):
    FILE = "file", "File"
    FOLDER = "folder", "Folder"


class ItemState(models.TextChoices):
    ACTIVE = "active", "Active"
    DELETED_UPSTREAM = "deleted_upstream", "Deleted Upstream"
    MISSING_UPSTREAM = "missing_upstream", "Missing Upstream"
    QUARANTINED = "quarantined", "Quarantined"
    PURGED = "purged", "Purged"


class VersionReason(models.TextChoices):
    UPDATE = "update", "Update"
    PRE_DELETE = "pre_delete", "Pre-Delete"
    MANUAL_SNAPSHOT = "manual_snapshot", "Manual Snapshot"
    CONFLICT = "conflict", "Conflict"
    RESTORE_POINT = "restore_point", "Restore Point"


class Account(models.Model):
    """
    Represents a cloud storage account.

    OAuth tokens are stored externally in the secrets file,
    not in the database. See backup/secrets.py.
    """

    provider = models.CharField(max_length=20, choices=Provider.choices)
    name = models.CharField(max_length=255)
    email = models.EmailField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Per-account sync scheduling
    sync_interval_minutes = models.PositiveIntegerField(
        default=360,  # 6 hours
        help_text="Minutes between syncs. 0 = use global schedule only."
    )
    next_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Next scheduled sync time."
    )

    class Meta:
        unique_together = [["provider", "email"]]
        indexes = [
            models.Index(fields=["is_active", "next_sync_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_provider_display()})"


class SyncRoot(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="sync_roots"
    )
    provider_root_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    sync_cursor = models.TextField(blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["account", "provider_root_id"]]

    def __str__(self):
        return f"{self.name} ({self.account})"


class BackupItem(models.Model):
    sync_root = models.ForeignKey(
        SyncRoot, on_delete=models.CASCADE, related_name="items"
    )
    provider_item_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    path = models.TextField()
    item_type = models.CharField(max_length=10, choices=ItemType.choices)
    mime_type = models.CharField(max_length=255, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    provider_modified_at = models.DateTimeField(null=True, blank=True)
    etag = models.CharField(max_length=255, blank=True)
    state = models.CharField(
        max_length=20, choices=ItemState.choices, default=ItemState.ACTIVE
    )
    state_changed_at = models.DateTimeField(auto_now_add=True)
    missing_since_sync_count = models.PositiveIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["sync_root", "provider_item_id"]]
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["provider_item_id"]),
            models.Index(fields=["sync_root", "state"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_state_display()})"


class BackupBlob(models.Model):
    digest = models.CharField(max_length=71, primary_key=True)  # sha256:<64 hex chars>
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="blobs"
    )
    size_bytes = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "created_at"]),
        ]

    def __str__(self):
        return f"{self.digest[:20]}... ({self.size_bytes} bytes)"


class FileVersion(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name="versions"
    )
    backup_item = models.ForeignKey(
        BackupItem, on_delete=models.CASCADE, related_name="versions"
    )
    blob = models.ForeignKey(
        BackupBlob, on_delete=models.PROTECT, related_name="versions"
    )
    observed_path = models.TextField()
    etag_or_revision = models.CharField(max_length=255, blank=True)
    content_modified_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=20, choices=VersionReason.choices)

    class Meta:
        indexes = [
            models.Index(fields=["backup_item", "captured_at"]),
            models.Index(fields=["account", "captured_at"]),
        ]

    def __str__(self):
        return f"{self.backup_item.name} @ {self.captured_at}"


class RetentionPolicy(models.Model):
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="retention_policies",
    )
    sync_root = models.ForeignKey(
        SyncRoot,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="retention_policies",
    )
    keep_last_n = models.PositiveIntegerField(default=10)
    keep_days = models.PositiveIntegerField(default=30)
    max_storage_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Retention policies"

    def __str__(self):
        target = self.sync_root or self.account or "Global"
        return f"Retention: {self.keep_last_n} versions / {self.keep_days} days ({target})"
