"""
Exceptions for sync operations.
"""


class SyncError(Exception):
    """Base exception for sync operations."""

    pass


class SyncAbortedError(SyncError):
    """Sync was aborted (e.g., account disabled, token refresh failed)."""

    pass


class TokenRefreshError(SyncError):
    """Failed to refresh OAuth token."""

    pass


class DownloadError(SyncError):
    """Failed to download file content."""

    pass


class StorageError(SyncError):
    """Failed to write to blob storage."""

    pass


class PathConflictError(SyncError):
    """Path conflict that couldn't be resolved."""

    pass
