# Local Drive

Django 6 application that backs up cloud storage providers (Google Drive, etc.) to local storage using content-addressed storage (CAS) with file versioning.

## Quick Start

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate

# Run
python manage.py runserver
```

## Configuration

1. Copy `secrets.json.example` to `.secrets.json` (chmod 600)
2. Add OAuth credentials:
```json
{
  "oauth_clients": {
    "google": { "client_id": "...", "client_secret": "..." }
  }
}
```
3. Copy `.env.example` to `.env` and configure `ALLOWED_HOSTS`

## Usage

```bash
python manage.py discover_accounts          # Create accounts from secrets
python manage.py sync_account <account_id>  # Sync an account
python manage.py sync_account <id> --force-initial  # Force full resync
```

## Docker

```bash
docker-compose up --build
```

## Tests

```bash
python manage.py test backup
```

## Architecture

Files stored by SHA256 digest in content-addressed storage (`blobs/sha256/aa/bb/<digest>`). Safe deletion uses ACTIVE -> MISSING_UPSTREAM -> QUARANTINED state transitions before archival.

See `docs/adr/` for architectural decision records.
