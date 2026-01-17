"""
Path building and resolution for sync operations.

Handles converting provider file hierarchy into filesystem paths,
with conflict resolution and sanitization.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backup.models import SyncRoot
    from backup.providers.google_drive import DriveFile

from backup.models import BackupItem

logger = logging.getLogger(__name__)


class PathBuilder:
    """
    Builds filesystem paths from provider file hierarchy.

    Handles:
    - Parent folder resolution
    - Name sanitization (invalid characters)
    - Conflict resolution (duplicate names)
    - Path caching for performance
    """

    # Characters forbidden in filenames on most filesystems
    INVALID_CHARS = '<>:"|?*\x00'

    def __init__(self, sync_root: SyncRoot):
        self.sync_root = sync_root
        self._path_cache: dict[str, str] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load existing paths from database into memory cache."""
        items = BackupItem.objects.filter(sync_root=self.sync_root).values(
            "provider_item_id", "path"
        )

        for item in items:
            self._path_cache[item["provider_item_id"]] = item["path"]

        logger.debug(f"Loaded {len(self._path_cache)} paths into cache")

    def build_path(self, drive_file: DriveFile) -> str:
        """
        Build relative filesystem path for a file.

        Args:
            drive_file: The Drive file to build a path for

        Returns:
            Relative path string (e.g., "Documents/report.pdf")
        """
        # Check cache first
        if drive_file.id in self._path_cache:
            return self._path_cache[drive_file.id]

        # Root-level file (no parents or parent is "root")
        if not drive_file.parents or "root" in drive_file.parents:
            path = self._sanitize_name(drive_file.name)
            path = self._resolve_conflicts(path, drive_file.id)
            self._path_cache[drive_file.id] = path
            return path

        # Get parent path
        parent_id = drive_file.parents[0]  # Use first parent

        if parent_id in self._path_cache:
            parent_path = self._path_cache[parent_id]
        else:
            # Try to fetch parent from database
            try:
                parent_item = BackupItem.objects.get(
                    sync_root=self.sync_root,
                    provider_item_id=parent_id,
                )
                parent_path = parent_item.path
                self._path_cache[parent_id] = parent_path
            except BackupItem.DoesNotExist:
                # Parent not yet synced, use temporary path
                logger.warning(
                    f"Parent {parent_id} not found for {drive_file.name}, using temp path"
                )
                parent_path = f"_pending_/{parent_id}"

        # Build full path
        name = self._sanitize_name(drive_file.name)
        path = f"{parent_path}/{name}"
        path = self._resolve_conflicts(path, drive_file.id)

        self._path_cache[drive_file.id] = path
        return path

    def _sanitize_name(self, name: str) -> str:
        """
        Remove invalid filesystem characters from a filename.

        Args:
            name: Original filename

        Returns:
            Sanitized filename
        """
        # Replace invalid chars with underscore
        for char in self.INVALID_CHARS:
            name = name.replace(char, "_")

        # Remove leading/trailing whitespace and dots
        name = name.strip(". ")

        # Ensure not empty
        if not name:
            name = "unnamed"

        # Limit length (preserving extension)
        if len(name) > 255:
            # Try to preserve extension
            parts = name.rsplit(".", 1)
            if len(parts) == 2 and len(parts[1]) <= 10:
                # Has reasonable extension
                max_base = 255 - len(parts[1]) - 1
                name = parts[0][:max_base] + "." + parts[1]
            else:
                # No extension or extension too long
                name = name[:255]

        return name

    def _resolve_conflicts(self, path: str, file_id: str) -> str:
        """
        Handle path conflicts by appending a counter.

        Args:
            path: Proposed path
            file_id: Provider file ID (to exclude self from conflict check)

        Returns:
            Unique path (possibly with counter appended)
        """
        original_path = path
        counter = 1

        while True:
            # Check if path is already used by a different file
            existing = (
                BackupItem.objects.filter(
                    sync_root=self.sync_root,
                    path=path,
                )
                .exclude(provider_item_id=file_id)
                .first()
            )

            if not existing:
                return path

            # Append counter to create unique path
            if "." in original_path and "/" not in original_path.rsplit(".", 1)[1]:
                # Has file extension
                parts = original_path.rsplit(".", 1)
                path = f"{parts[0]} ({counter}).{parts[1]}"
            else:
                # No extension or extension has slash (directory-like)
                path = f"{original_path} ({counter})"

            counter += 1

            # Safety: prevent infinite loop
            if counter > 1000:
                logger.error(f"Too many conflicts for path: {original_path}")
                # Use file ID as last resort
                path = f"{original_path}_{file_id}"
                break

        return path

    def refresh_cache(self) -> None:
        """Rebuild path cache from database."""
        self._path_cache.clear()
        self._load_cache()
