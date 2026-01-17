# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local Drive is a Django 6 application that backs up cloud storage providers (Google Drive, etc.) to local storage using content-addressed storage (CAS) with file versioning.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate

# Run development server
python manage.py runserver

# Run all tests
python manage.py test backup

# Run specific test module
python manage.py test backup.tests.test_storage
python manage.py test backup.tests.test_secrets
python manage.py test backup.tests.test_google_drive

# Run single test
python manage.py test backup.tests.test_storage.AccountStorageTests.test_write_blob

# Management commands
python manage.py discover_accounts          # Create accounts from secrets.json
python manage.py sync_account <account_id>  # Run sync for an account
python manage.py sync_account <id> --force-initial  # Force full resync

# Docker
docker-compose up --build
```

## Architecture

### Core Design Principles

1. **Content-Addressed Storage (CAS)**: Files stored by SHA256 digest in `blobs/sha256/aa/bb/<digest>`. Enables deduplication and integrity verification.

2. **External Secrets**: OAuth tokens stored in `.secrets.json` (chmod 600), never in database. Access via `backup/secrets.py`.

3. **Safe Deletion**: Files missing from upstream go through ACTIVE -> MISSING_UPSTREAM -> QUARANTINED states (2-sync threshold) before archival. See `docs/adr/` for details.

### Storage Layout

```
BACKUP_ROOT/<provider>/<account_id>/
    blobs/       # Content-addressed storage
    current/     # Materialized backup tree
    archive/     # Quarantined files
    tmp/         # Atomic write staging
```

### Key Modules

- `backup/models.py` - Account, SyncRoot, BackupItem, BackupBlob, FileVersion, RetentionPolicy
- `backup/storage.py` - AccountStorage class for CAS operations
- `backup/secrets.py` - Token management (get_tokens, set_tokens, has_tokens)
- `backup/providers/google_drive.py` - GoogleDriveClient with OAuth and changes API
- `backup/sync/engine.py` - SyncEngine for initial/incremental sync orchestration
- `backup/views/oauth.py` - OAuth callback handlers

### Sync Flow

1. SyncEngine determines initial vs incremental sync
2. Fetches changes from provider's changes API
3. Downloads files to blob storage, creates FileVersion records
4. Materializes blobs to `current/` directory
5. Updates deletion states for missing files

## Configuration

Environment variables (or `.env` file):
- `SECRETS_FILE` - Path to secrets JSON (default: `.secrets.json`)
- `ALLOWED_HOSTS` - Comma-separated allowed hosts

OAuth credentials go in `secrets.json` under `oauth_clients.google`:
```json
{
  "oauth_clients": {
    "google": {
      "client_id": "...",
      "client_secret": "..."
    }
  }
}
```

## Critical Patterns

Token storage - always use secrets module:
```python
# Correct
from backup import secrets
secrets.set_tokens(account, access_token=token, refresh_token=refresh)

# Wrong - Account has no token fields
account.access_token = token  # AttributeError
```

Blob operations:
```python
storage = AccountStorage(account)
digest = storage.write_blob(content)  # Returns sha256:...
storage.materialize_to_current(digest, "path/to/file.txt")
```

## Testing

Tests use in-memory SQLite and mocked Google API via `unittest.mock`. Secrets must be set up before testing GoogleDriveClient:
```python
secrets.set_tokens(account, access_token="test", refresh_token="test")
```
