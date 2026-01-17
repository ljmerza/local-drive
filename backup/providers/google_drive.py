"""
Google Drive API client for backup operations.

Provides OAuth authentication, file listing via Changes API,
and file download functionality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import TYPE_CHECKING, BinaryIO, Iterator

from django.conf import settings
from django.utils import timezone as dj_timezone
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

if TYPE_CHECKING:
    from backup.models import Account

from backup import secrets

logger = logging.getLogger(__name__)

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Google Docs MIME types that need export
GOOGLE_DOC_TYPES = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.form": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.script": ("application/vnd.google-apps.script+json", ".json"),
}

# MIME types that cannot be downloaded (shortcuts, etc.)
NON_DOWNLOADABLE_TYPES = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.fusiontable",
}


class GoogleDriveError(Exception):
    """Base exception for Google Drive operations."""

    pass


class TokenExpiredError(GoogleDriveError):
    """Raised when token refresh fails."""

    pass


class FileNotDownloadableError(GoogleDriveError):
    """Raised when a file type cannot be downloaded."""

    pass


@dataclass
class DriveFile:
    """Represents a file from Google Drive."""

    id: str
    name: str
    mime_type: str
    size: int | None
    modified_time: datetime
    md5_checksum: str | None
    parents: list[str]
    trashed: bool
    etag: str | None = None

    @classmethod
    def from_api_response(cls, data: dict) -> "DriveFile":
        """Create DriveFile from Google API response."""
        modified_time = datetime.fromisoformat(
            data["modifiedTime"].replace("Z", "+00:00")
        )
        return cls(
            id=data["id"],
            name=data["name"],
            mime_type=data.get("mimeType", ""),
            size=int(data["size"]) if "size" in data else None,
            modified_time=modified_time,
            md5_checksum=data.get("md5Checksum"),
            parents=data.get("parents", []),
            trashed=data.get("trashed", False),
            etag=data.get("etag"),
        )

    @property
    def is_folder(self) -> bool:
        return self.mime_type == "application/vnd.google-apps.folder"

    @property
    def is_google_doc(self) -> bool:
        return self.mime_type in GOOGLE_DOC_TYPES

    @property
    def is_downloadable(self) -> bool:
        return self.mime_type not in NON_DOWNLOADABLE_TYPES

    @property
    def export_mime_type(self) -> str | None:
        """Return the export MIME type for Google Docs, or None if not a Doc."""
        if self.mime_type in GOOGLE_DOC_TYPES:
            return GOOGLE_DOC_TYPES[self.mime_type][0]
        return None

    @property
    def export_extension(self) -> str | None:
        """Return the file extension for exported Google Docs."""
        if self.mime_type in GOOGLE_DOC_TYPES:
            return GOOGLE_DOC_TYPES[self.mime_type][1]
        return None


@dataclass
class DriveChange:
    """Represents a change from the Google Drive Changes API."""

    file_id: str
    removed: bool
    file: DriveFile | None
    change_type: str  # 'file' or 'drive'
    time: datetime | None

    @classmethod
    def from_api_response(cls, data: dict) -> "DriveChange":
        """Create DriveChange from Google API response."""
        file_data = data.get("file")
        time_str = data.get("time")
        return cls(
            file_id=data.get("fileId", ""),
            removed=data.get("removed", False),
            file=DriveFile.from_api_response(file_data) if file_data else None,
            change_type=data.get("changeType", "file"),
            time=datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if time_str
            else None,
        )


@dataclass
class ChangesPage:
    """A page of changes from the Changes API."""

    changes: list[DriveChange]
    new_start_page_token: str | None
    next_page_token: str | None

    @property
    def has_more(self) -> bool:
        return self.next_page_token is not None


def create_oauth_flow(state: str | None = None) -> Flow:
    """
    Create an OAuth flow for Google Drive authentication.

    Args:
        state: Optional state parameter for CSRF protection

    Returns:
        Configured OAuth Flow object
    """
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = settings.GOOGLE_REDIRECT_URI
    return flow


def get_authorization_url(state: str | None = None) -> tuple[str, str]:
    """
    Generate the Google OAuth authorization URL.

    Args:
        state: Optional state for CSRF protection

    Returns:
        Tuple of (authorization_url, state)
    """
    flow = create_oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return authorization_url, state


def exchange_code_for_tokens(code: str) -> dict:
    """
    Exchange an authorization code for access and refresh tokens.

    Args:
        code: The authorization code from OAuth callback

    Returns:
        Dict with access_token, refresh_token, expires_at, email, name
    """
    flow = create_oauth_flow()
    flow.fetch_token(code=code)
    credentials = flow.credentials

    # Get user info
    service = build("oauth2", "v2", credentials=credentials)
    user_info = service.userinfo().get().execute()

    return {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "expires_at": credentials.expiry,
        "email": user_info.get("email"),
        "name": user_info.get("name", user_info.get("email", "Unknown")),
    }


class GoogleDriveClient:
    """
    Client for Google Drive API operations.

    Handles authentication, token refresh, file listing,
    and file downloads.
    """

    def __init__(self, account: "Account"):
        """
        Initialize the client with an account.

        Args:
            account: Account model instance with stored credentials
        """
        self.account = account
        self._credentials: Credentials | None = None
        self._service = None

    def _get_credentials(self) -> Credentials:
        """Get or create credentials from secrets file."""
        if self._credentials is None:
            tokens = secrets.get_tokens(self.account)
            if tokens is None:
                raise TokenExpiredError(
                    f"No tokens found for account {self.account.email}"
                )

            self._credentials = Credentials(
                token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET,
                expiry=tokens.get("expires_at"),
            )
        return self._credentials

    def refresh_token_if_needed(self) -> bool:
        """
        Refresh the access token if expired or expiring soon.

        Returns:
            True if token was refreshed, False otherwise

        Raises:
            TokenExpiredError: If refresh fails
        """
        credentials = self._get_credentials()

        # Check if refresh is needed (expired or expiring in next 5 minutes)
        if credentials.expiry:
            buffer = timedelta(minutes=5)
            if credentials.expiry > datetime.now(timezone.utc) + buffer:
                return False

        if not credentials.refresh_token:
            raise TokenExpiredError("No refresh token available")

        try:
            credentials.refresh(Request())

            # Save new tokens to secrets file
            secrets.set_tokens(
                self.account,
                access_token=credentials.token,
                refresh_token=credentials.refresh_token or credentials._refresh_token,
                expires_at=credentials.expiry,
            )

            logger.info(f"Refreshed token for account {self.account.id}")
            return True

        except Exception as e:
            logger.error(f"Token refresh failed for account {self.account.id}: {e}")
            raise TokenExpiredError(f"Token refresh failed: {e}") from e

    def _get_service(self):
        """Get or create the Drive API service."""
        if self._service is None:
            self.refresh_token_if_needed()
            credentials = self._get_credentials()
            self._service = build("drive", "v3", credentials=credentials)
        return self._service

    def get_about(self) -> dict:
        """
        Get information about the user and their Drive.

        Returns:
            Dict with user email, display name, storage quota
        """
        service = self._get_service()
        about = service.about().get(fields="user,storageQuota").execute()
        return {
            "email": about["user"].get("emailAddress"),
            "display_name": about["user"].get("displayName"),
            "storage_quota": about.get("storageQuota", {}),
        }

    def get_user_info(self) -> dict:
        """
        Get basic user info for connection testing.

        Lighter weight than get_about() - only fetches user info.

        Returns:
            Dict with email and display_name

        Raises:
            TokenExpiredError: If authentication fails
        """
        service = self._get_service()
        about = service.about().get(fields="user").execute()
        return {
            "email": about["user"].get("emailAddress"),
            "display_name": about["user"].get("displayName"),
        }

    def get_start_page_token(self, drive_id: str | None = None) -> str:
        """
        Get the starting page token for the Changes API.

        Args:
            drive_id: Optional shared drive ID

        Returns:
            The start page token string
        """
        service = self._get_service()
        params = {}
        if drive_id:
            params["driveId"] = drive_id
            params["supportsAllDrives"] = True

        response = service.changes().getStartPageToken(**params).execute()
        return response["startPageToken"]

    def list_changes(
        self,
        page_token: str,
        page_size: int = 1000,
        drive_id: str | None = None,
    ) -> ChangesPage:
        """
        List changes since the given page token.

        Args:
            page_token: The page token from previous call or getStartPageToken
            page_size: Number of changes per page (max 1000)
            drive_id: Optional shared drive ID

        Returns:
            ChangesPage with changes and next/new tokens
        """
        service = self._get_service()

        params = {
            "pageToken": page_token,
            "pageSize": page_size,
            "fields": (
                "nextPageToken,newStartPageToken,"
                "changes(fileId,removed,changeType,time,"
                "file(id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed))"
            ),
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
        }
        if drive_id:
            params["driveId"] = drive_id

        response = service.changes().list(**params).execute()

        changes = [
            DriveChange.from_api_response(c) for c in response.get("changes", [])
        ]

        return ChangesPage(
            changes=changes,
            new_start_page_token=response.get("newStartPageToken"),
            next_page_token=response.get("nextPageToken"),
        )

    def iter_all_changes(
        self,
        start_token: str,
        drive_id: str | None = None,
    ) -> Iterator[tuple[list[DriveChange], str]]:
        """
        Iterate through all changes, yielding batches with the latest token.

        Args:
            start_token: The starting page token
            drive_id: Optional shared drive ID

        Yields:
            Tuple of (list of changes, current_token)
        """
        page_token = start_token

        while True:
            page = self.list_changes(page_token, drive_id=drive_id)
            current_token = page.new_start_page_token or page.next_page_token or page_token

            if page.changes:
                yield page.changes, current_token

            if page.new_start_page_token:
                # No more changes
                break
            elif page.next_page_token:
                page_token = page.next_page_token
            else:
                break

    def get_file_metadata(self, file_id: str) -> DriveFile:
        """
        Get metadata for a single file.

        Args:
            file_id: The Google Drive file ID

        Returns:
            DriveFile object with file metadata
        """
        service = self._get_service()
        response = (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed",
                supportsAllDrives=True,
            )
            .execute()
        )
        return DriveFile.from_api_response(response)

    def download_file(self, file_id: str) -> BytesIO:
        """
        Download a file's content.

        Args:
            file_id: The Google Drive file ID

        Returns:
            BytesIO object with file contents

        Raises:
            FileNotDownloadableError: If file type cannot be downloaded
        """
        service = self._get_service()

        # Get file metadata to check type
        file_meta = self.get_file_metadata(file_id)

        if not file_meta.is_downloadable:
            raise FileNotDownloadableError(
                f"File type {file_meta.mime_type} cannot be downloaded"
            )

        buffer = BytesIO()

        if file_meta.is_google_doc:
            # Export Google Docs
            export_mime = file_meta.export_mime_type
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            # Regular file download
            request = service.files().get_media(fileId=file_id)

        downloader = MediaIoBaseDownload(buffer, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                logger.debug(f"Download progress: {int(status.progress() * 100)}%")

        buffer.seek(0)
        return buffer

    def download_file_to_stream(
        self, file_id: str, stream: BinaryIO
    ) -> int:
        """
        Download a file's content to a provided stream.

        Args:
            file_id: The Google Drive file ID
            stream: Writable binary stream

        Returns:
            Number of bytes written

        Raises:
            FileNotDownloadableError: If file type cannot be downloaded
        """
        service = self._get_service()
        file_meta = self.get_file_metadata(file_id)

        if not file_meta.is_downloadable:
            raise FileNotDownloadableError(
                f"File type {file_meta.mime_type} cannot be downloaded"
            )

        if file_meta.is_google_doc:
            export_mime = file_meta.export_mime_type
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            request = service.files().get_media(fileId=file_id)

        downloader = MediaIoBaseDownload(stream, request)

        done = False
        bytes_downloaded = 0
        while not done:
            status, done = downloader.next_chunk()
            if status:
                bytes_downloaded = status.resumable_progress

        return bytes_downloaded

    def list_files_in_folder(
        self,
        folder_id: str = "root",
        page_size: int = 1000,
    ) -> Iterator[DriveFile]:
        """
        List all files in a folder (non-recursive).

        Args:
            folder_id: The folder ID (default: root)
            page_size: Number of files per page

        Yields:
            DriveFile objects
        """
        service = self._get_service()
        page_token = None

        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "pageSize": page_size,
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum,parents,trashed)",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if page_token:
                params["pageToken"] = page_token

            response = service.files().list(**params).execute()

            for file_data in response.get("files", []):
                yield DriveFile.from_api_response(file_data)

            page_token = response.get("nextPageToken")
            if not page_token:
                break
