"""
Content-addressed blob storage for backup files.

Storage layout per account:
    BACKUP_ROOT/<provider>/<account_id>/
        current/     - Human-browsable backup tree
        blobs/sha256/aa/bb/<digest>  - Immutable content blobs
        tmp/         - In-progress writes
        archive/     - Quarantined files
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from django.conf import settings

if TYPE_CHECKING:
    from .models import Account


class DigestError(Exception):
    """Raised when digest verification fails."""

    pass


class BlobNotFoundError(Exception):
    """Raised when a blob does not exist."""

    pass


def parse_digest(digest: str) -> tuple[str, str]:
    """
    Parse digest string into (algorithm, hex_value).

    Args:
        digest: Digest in format "sha256:<hex>"

    Returns:
        Tuple of (algorithm, hex_value)

    Raises:
        ValueError: If digest format is invalid
    """
    if ":" not in digest:
        raise ValueError(f"Invalid digest format: {digest}")
    algo, hex_value = digest.split(":", 1)
    if algo != "sha256":
        raise ValueError(f"Unsupported digest algorithm: {algo}")
    if len(hex_value) != 64:
        raise ValueError(f"Invalid digest length: {len(hex_value)}")
    return algo, hex_value


def compute_digest(data: bytes | BinaryIO) -> str:
    """
    Compute SHA256 digest of data.

    Args:
        data: Bytes or file-like object to hash

    Returns:
        Digest string in format "sha256:<hex>"
    """
    hasher = hashlib.sha256()
    if isinstance(data, bytes):
        hasher.update(data)
    else:
        for chunk in iter(lambda: data.read(65536), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


class VerifyingReader:
    """
    File wrapper that verifies digest on close or when fully read.
    """

    def __init__(self, file: BinaryIO, expected_digest: str):
        self._file = file
        self._expected_digest = expected_digest
        self._hasher = hashlib.sha256()
        self._verified = False

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        if data:
            self._hasher.update(data)
        elif not self._verified:
            self._verify()
        return data

    def _verify(self) -> None:
        if self._verified:
            return
        self._verified = True
        actual = f"sha256:{self._hasher.hexdigest()}"
        if actual != self._expected_digest:
            raise DigestError(
                f"Digest mismatch: expected {self._expected_digest}, got {actual}"
            )

    def close(self) -> None:
        if not self._verified:
            # Read remaining data to verify
            while self._file.read(65536):
                pass
            self._verify()
        self._file.close()

    def __enter__(self) -> "VerifyingReader":
        return self

    def __exit__(self, *args) -> None:
        self.close()


class AccountStorage:
    """
    Content-addressed storage for a single account.

    Provides atomic blob writes, digest verification, and
    materialization to the browsable backup tree.
    """

    def __init__(self, account: Account):
        self.account = account
        self.root = Path(settings.BACKUP_ROOT) / account.provider / str(account.id)
        self.current_dir = self.root / "current"
        self.blobs_dir = self.root / "blobs"
        self.tmp_dir = self.root / "tmp"
        self.archive_dir = self.root / "archive"

    def ensure_directories(self) -> None:
        """Create the account directory structure if it doesn't exist."""
        self.current_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def get_blob_path(self, digest: str) -> Path:
        """
        Compute the filesystem path for a blob.

        Uses sharding: blobs/sha256/aa/bb/<full_digest>
        where aa and bb are first 2 bytes of the hex digest.
        """
        algo, hex_value = parse_digest(digest)
        return self.blobs_dir / algo / hex_value[:2] / hex_value[2:4] / hex_value

    def blob_exists(self, digest: str) -> bool:
        """Check if a blob exists on disk."""
        return self.get_blob_path(digest).exists()

    def write_blob(
        self, data: bytes | BinaryIO, expected_digest: str | None = None
    ) -> str:
        """
        Write content to blob storage atomically.

        Args:
            data: Bytes or file-like object containing content
            expected_digest: Optional expected digest for verification

        Returns:
            The digest of the written content

        Raises:
            DigestError: If expected_digest doesn't match actual content
        """
        self.ensure_directories()

        # Generate temp file path
        tmp_path = self.tmp_dir / f"{uuid.uuid4().hex}.tmp"

        try:
            # Write to temp file while computing digest
            hasher = hashlib.sha256()
            size = 0

            with open(tmp_path, "wb") as f:
                if isinstance(data, bytes):
                    hasher.update(data)
                    f.write(data)
                    size = len(data)
                else:
                    for chunk in iter(lambda: data.read(65536), b""):
                        hasher.update(chunk)
                        f.write(chunk)
                        size += len(chunk)
                # Ensure data is flushed to disk
                f.flush()
                os.fsync(f.fileno())

            digest = f"sha256:{hasher.hexdigest()}"

            # Verify if expected digest was provided
            if expected_digest and digest != expected_digest:
                raise DigestError(
                    f"Digest mismatch: expected {expected_digest}, got {digest}"
                )

            # Move to final location
            blob_path = self.get_blob_path(digest)
            if not blob_path.exists():
                blob_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path.rename(blob_path)
                # Make blob read-only
                blob_path.chmod(0o444)
            else:
                # Blob already exists, remove temp file
                tmp_path.unlink()

            return digest

        except Exception:
            # Clean up temp file on error
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def read_blob(self, digest: str, verify: bool = True) -> BinaryIO:
        """
        Read a blob from storage.

        Args:
            digest: The digest of the blob to read
            verify: If True, verify digest while reading

        Returns:
            File-like object for reading blob content

        Raises:
            BlobNotFoundError: If blob doesn't exist
            DigestError: If verification fails (only when verify=True)
        """
        blob_path = self.get_blob_path(digest)
        if not blob_path.exists():
            raise BlobNotFoundError(f"Blob not found: {digest}")

        file = open(blob_path, "rb")
        if verify:
            return VerifyingReader(file, digest)
        return file

    def read_blob_bytes(self, digest: str, verify: bool = True) -> bytes:
        """
        Read a blob and return its contents as bytes.

        Args:
            digest: The digest of the blob to read
            verify: If True, verify digest

        Returns:
            Blob content as bytes
        """
        with self.read_blob(digest, verify=verify) as f:
            return f.read()

    def delete_blob(self, digest: str) -> bool:
        """
        Delete a blob from storage.

        Args:
            digest: The digest of the blob to delete

        Returns:
            True if blob was deleted, False if it didn't exist
        """
        blob_path = self.get_blob_path(digest)
        if blob_path.exists():
            # Remove read-only protection before deleting
            blob_path.chmod(0o644)
            blob_path.unlink()
            # Clean up empty parent directories
            self._cleanup_empty_dirs(blob_path.parent)
            return True
        return False

    def _cleanup_empty_dirs(self, path: Path) -> None:
        """Remove empty directories up to blobs_dir."""
        while path != self.blobs_dir and path.exists():
            try:
                path.rmdir()
                path = path.parent
            except OSError:
                # Directory not empty
                break

    def get_current_path(self, relative_path: str) -> Path:
        """
        Get the absolute filesystem path for a relative path in current/ tree.

        Args:
            relative_path: Path relative to current/ directory

        Returns:
            Absolute Path object for the file in current/ directory
        """
        return self.current_dir / relative_path

    def materialize_to_current(
        self, digest: str, relative_path: str, use_hardlink: bool = False
    ) -> Path:
        """
        Copy or hardlink a blob to the current/ tree.

        Args:
            digest: The digest of the blob to materialize
            relative_path: Path relative to current/ directory
            use_hardlink: If True, use hardlink instead of copy

        Returns:
            The absolute path of the materialized file

        Raises:
            BlobNotFoundError: If blob doesn't exist
        """
        blob_path = self.get_blob_path(digest)
        if not blob_path.exists():
            raise BlobNotFoundError(f"Blob not found: {digest}")

        target_path = self.current_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing file if present
        if target_path.exists():
            target_path.unlink()

        if use_hardlink:
            try:
                os.link(blob_path, target_path)
            except OSError:
                # Fallback to copy if hardlink fails (cross-filesystem)
                shutil.copy2(blob_path, target_path)
        else:
            shutil.copy2(blob_path, target_path)

        return target_path

    def remove_from_current(self, relative_path: str) -> bool:
        """
        Remove a file from the current/ tree.

        Args:
            relative_path: Path relative to current/ directory

        Returns:
            True if file was removed, False if it didn't exist
        """
        target_path = self.current_dir / relative_path
        if target_path.exists():
            target_path.unlink()
            # Clean up empty parent directories
            self._cleanup_empty_dirs_to(target_path.parent, self.current_dir)
            return True
        return False

    def _cleanup_empty_dirs_to(self, path: Path, stop_at: Path) -> None:
        """Remove empty directories up to stop_at."""
        while path != stop_at and path.exists():
            try:
                path.rmdir()
                path = path.parent
            except OSError:
                break

    def move_to_archive(self, relative_path: str) -> Path | None:
        """
        Move a file from current/ to archive/.

        Args:
            relative_path: Path relative to current/ directory

        Returns:
            The new path in archive/, or None if source didn't exist
        """
        source_path = self.current_dir / relative_path
        if not source_path.exists():
            return None

        target_path = self.archive_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Handle existing file in archive
        if target_path.exists():
            target_path.unlink()

        shutil.move(str(source_path), str(target_path))
        self._cleanup_empty_dirs_to(source_path.parent, self.current_dir)
        return target_path

    def restore_from_archive(self, relative_path: str) -> Path | None:
        """
        Move a file from archive/ back to current/.

        Args:
            relative_path: Path relative to archive/ directory

        Returns:
            The new path in current/, or None if source didn't exist
        """
        source_path = self.archive_dir / relative_path
        if not source_path.exists():
            return None

        target_path = self.current_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            target_path.unlink()

        shutil.move(str(source_path), str(target_path))
        self._cleanup_empty_dirs_to(source_path.parent, self.archive_dir)
        return target_path

    def get_storage_stats(self) -> dict:
        """
        Get storage statistics for this account.

        Returns:
            Dict with blob_count, total_size_bytes, current_file_count
        """
        blob_count = 0
        total_size = 0
        current_files = 0

        # Count blobs
        if self.blobs_dir.exists():
            for blob_file in self.blobs_dir.rglob("*"):
                if blob_file.is_file():
                    blob_count += 1
                    total_size += blob_file.stat().st_size

        # Count current files
        if self.current_dir.exists():
            for f in self.current_dir.rglob("*"):
                if f.is_file():
                    current_files += 1

        return {
            "blob_count": blob_count,
            "total_size_bytes": total_size,
            "current_file_count": current_files,
        }
