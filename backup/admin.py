from django.contrib import admin

from .models import (
    Account,
    BackupBlob,
    BackupItem,
    FileVersion,
    RetentionPolicy,
    SyncRoot,
)
from .sync.models import SyncEvent, SyncSession


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "provider",
        "email",
        "is_active",
        "sync_interval_minutes",
        "next_sync_at",
        "created_at",
    ]
    list_filter = ["provider", "is_active"]
    list_editable = ["sync_interval_minutes"]
    search_fields = ["name", "email"]
    readonly_fields = ["next_sync_at"]


@admin.register(SyncRoot)
class SyncRootAdmin(admin.ModelAdmin):
    list_display = ["name", "account", "is_enabled", "last_sync_at"]
    list_filter = ["is_enabled", "account__provider"]
    search_fields = ["name", "provider_root_id"]


@admin.register(BackupItem)
class BackupItemAdmin(admin.ModelAdmin):
    list_display = ["name", "sync_root", "item_type", "state", "size_bytes", "updated_at"]
    list_filter = ["state", "item_type", "sync_root"]
    search_fields = ["name", "path", "provider_item_id"]
    raw_id_fields = ["parent"]


@admin.register(BackupBlob)
class BackupBlobAdmin(admin.ModelAdmin):
    list_display = ["digest", "account", "size_bytes", "created_at"]
    list_filter = ["account"]
    search_fields = ["digest"]


@admin.register(FileVersion)
class FileVersionAdmin(admin.ModelAdmin):
    list_display = ["backup_item", "reason", "captured_at", "blob"]
    list_filter = ["reason", "account"]
    search_fields = ["observed_path", "backup_item__name"]
    raw_id_fields = ["backup_item", "blob"]


@admin.register(RetentionPolicy)
class RetentionPolicyAdmin(admin.ModelAdmin):
    list_display = ["__str__", "keep_last_n", "keep_days", "max_storage_bytes"]
    list_filter = ["account"]


@admin.register(SyncSession)
class SyncSessionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "sync_root",
        "status",
        "is_initial",
        "started_at",
        "completed_at",
        "files_added",
        "files_updated",
        "files_deleted",
        "files_quarantined",
    ]
    list_filter = ["status", "is_initial", "started_at"]
    search_fields = ["sync_root__name", "sync_root__account__name"]
    readonly_fields = ["started_at", "completed_at", "start_cursor", "end_cursor"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("sync_root__account")


@admin.register(SyncEvent)
class SyncEventAdmin(admin.ModelAdmin):
    list_display = ["id", "session", "event_type", "timestamp", "file_path"]
    list_filter = ["event_type", "timestamp"]
    search_fields = ["file_path", "provider_file_id", "message"]
    readonly_fields = ["timestamp"]
    raw_id_fields = ["session", "backup_item"]
