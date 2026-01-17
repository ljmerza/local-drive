# Backend Implementation Plan: Manual Token Workflow

## Overview

Support workflow where users obtain OAuth tokens manually (via OAuth Playground) and add them to `secrets.json`, then create accounts in the app to use those tokens for background syncing.

## Current State Analysis

### ✅ Already Working
1. **secrets.py** - Has functions to read/write tokens from secrets.json
   - `get_tokens(account)` - Retrieves tokens for an account
   - `set_tokens(account, access_token, refresh_token, expires_at)` - Stores tokens
   - `has_tokens(account)` - Checks if tokens exist
   - Key format: `{provider}:{email}` (e.g., `google:user@example.com`)

2. **OAuth client credentials** - Now stored in secrets.json
   - `get_oauth_client_config(provider)` - Gets client ID/secret
   - Settings.py reads from secrets.json first, falls back to env vars

3. **GoogleDriveClient** - Already uses tokens from secrets.json
   - File: `backup/providers/google_drive.py`
   - Gets tokens via `secrets.get_tokens(account)`
   - Automatically refreshes expired access tokens using refresh token

4. **sync_account command** - Already works with manual tokens
   - File: `backup/management/commands/sync_account.py`
   - Takes account ID and syncs using tokens from secrets.json

### ❌ What's Missing

1. **Easy way to create accounts** - Currently requires Django shell
   - Need: Management command to create Account records easily
   - Should validate that tokens exist in secrets.json

2. **Token validation** - No way to test if tokens work before syncing
   - Need: Command to verify tokens are valid

3. **Account listing** - No easy way to see accounts and their token status
   - Need: Command to list accounts and check token availability

## Implementation Tasks

### Task 1: Create `add_account` Management Command

**File:** `backup/management/commands/add_account.py`

**Purpose:** Create an Account record that matches tokens in secrets.json

**Usage:**
```bash
python manage.py add_account \
  --provider google \
  --email user@gmail.com \
  --name "My Google Drive"
```

**Features:**
- ✅ Creates Account with specified provider, email, name
- ✅ Validates that tokens exist in secrets.json for this account
- ✅ Checks for duplicate accounts (provider + email is unique)
- ✅ Creates default SyncRoot for "My Drive" (root folder)
- ✅ Returns account ID for use in sync commands

**Validation Logic:**
1. Check if account already exists (provider + email unique constraint)
2. Check if tokens exist in secrets.json using `secrets.has_tokens()`
3. Warn if tokens don't exist (allow creation anyway for manual setup)
4. Create Account record
5. Create default SyncRoot for the account

### Task 2: Create `verify_tokens` Management Command

**File:** `backup/management/commands/verify_tokens.py`

**Purpose:** Test if tokens are valid by making a test API call

**Usage:**
```bash
python manage.py verify_tokens <account_id>
```

**Features:**
- ✅ Loads account and tokens from secrets.json
- ✅ Creates GoogleDriveClient
- ✅ Makes test API call (get user info or list first page of files)
- ✅ Reports token status (valid/expired/invalid)
- ✅ Shows token expiration time if available
- ✅ Tests refresh token by attempting refresh if access token expired

**Output Example:**
```
Account: My Google Drive (user@gmail.com)
Status: ✓ Tokens are valid
Access Token: Valid (expires in 45 minutes)
Refresh Token: Present
Last API Test: ✓ Successfully listed files
```

### Task 3: Create `list_accounts` Management Command

**File:** `backup/management/commands/list_accounts.py`

**Purpose:** List all accounts with their sync status and token availability

**Usage:**
```bash
python manage.py list_accounts
```

**Features:**
- ✅ Lists all Account records
- ✅ Shows if tokens exist in secrets.json
- ✅ Shows last sync time for each SyncRoot
- ✅ Shows active/inactive status
- ✅ Highlights accounts missing tokens

**Output Example:**
```
ID  Provider      Email              Name               Tokens  Last Sync           Status
==  ============  =================  =================  ======  ==================  ========
1   Google Drive  user@gmail.com     My Google Drive    ✓       2026-01-06 10:30   Active
2   Google Drive  work@company.com   Work Drive         ✗       Never              Active
3   OneDrive      user@outlook.com   OneDrive Backup    ✓       2026-01-05 15:20   Inactive

Total: 3 accounts (2 with tokens, 1 active)
```

### Task 4: Update `secrets.json` Structure Documentation

**File:** `secrets.json.example`

**Purpose:** Show example with both OAuth clients and user tokens

**Current Example:**
```json
{
  "oauth_clients": {
    "google": {
      "client_id": "...",
      "client_secret": "...",
      "redirect_uri": "..."
    }
  }
}
```

**Updated Example:**
```json
{
  "oauth_clients": {
    "google": {
      "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
      "client_secret": "YOUR_CLIENT_SECRET",
      "redirect_uri": "http://localhost:8000/oauth/google/callback"
    }
  },
  "_comment": "User tokens below are added manually or via OAuth flow",
  "google:user@gmail.com": {
    "access_token": "ya29.a0AfB_...",
    "refresh_token": "1//0gXXX...",
    "expires_at": "2026-01-06T15:30:00"
  },
  "google:work@company.com": {
    "access_token": "ya29.a0AfB_...",
    "refresh_token": "1//0gYYY...",
    "expires_at": null
  }
}
```

## Workflow After Implementation

### Setup Workflow (One-time)

1. **Get OAuth client credentials** from Google Cloud Console
   - Add to `secrets.json` under `oauth_clients.google`

2. **Get user tokens** from OAuth Playground
   - Add to `secrets.json` under `google:user@email.com`

3. **Create account** in the app:
   ```bash
   python manage.py add_account \
     --provider google \
     --email user@email.com \
     --name "My Google Drive"
   ```
   Output: `Created account with ID: 1`

4. **Verify tokens work**:
   ```bash
   python manage.py verify_tokens 1
   ```
   Output: `✓ Tokens are valid`

5. **Run first sync**:
   ```bash
   python manage.py sync_account 1
   ```

### Daily Operation Workflow

**Automated via cron:**
```bash
# Crontab entry - sync every 6 hours
0 */6 * * * docker exec local-drive-app python manage.py sync_account 1
```

**Manual checks:**
```bash
# List all accounts and their status
python manage.py list_accounts

# Verify specific account tokens
python manage.py verify_tokens 1

# Run manual sync
python manage.py sync_account 1
```

## File Changes Summary

### New Files
- `backup/management/commands/add_account.py` - Create accounts easily
- `backup/management/commands/verify_tokens.py` - Test token validity
- `backup/management/commands/list_accounts.py` - List accounts with status
- `OAUTH_PLAYGROUND_GUIDE.md` - User guide for getting tokens (already created)
- `BACKEND_IMPLEMENTATION_PLAN.md` - This file

### Modified Files
- `secrets.json.example` - Add example user tokens section
- `DOCKER_SETUP.md` - Update with manual token workflow
- `README.md` (if exists) - Add links to guides

## Testing Plan

### Test Case 1: Happy Path
1. Add tokens to secrets.json manually
2. Run `add_account` command
3. Run `verify_tokens` command - should pass
4. Run `sync_account` command - should sync successfully
5. Run `list_accounts` - should show account with tokens

### Test Case 2: Missing Tokens
1. Run `add_account` without tokens in secrets.json
2. Should create account with warning
3. Run `verify_tokens` - should fail gracefully
4. Run `sync_account` - should error with helpful message

### Test Case 3: Expired Access Token
1. Set access_token to expired value in secrets.json
2. Keep valid refresh_token
3. Run `verify_tokens` - should auto-refresh and pass
4. Run `sync_account` - should work after refresh

### Test Case 4: Invalid Refresh Token
1. Set invalid refresh_token in secrets.json
2. Run `verify_tokens` - should fail with clear error
3. Should provide instructions to re-obtain tokens

## Benefits of This Approach

✅ **No UI needed** - Perfect for headless servers
✅ **No redirect URI issues** - OAuth Playground handles that
✅ **Simple deployment** - Just mount secrets.json
✅ **Network flexible** - Works from any IP (192.168.x.x, etc.)
✅ **Easy automation** - Management commands for everything
✅ **Clear workflow** - Well-documented steps
✅ **Backwards compatible** - Still supports web UI OAuth flow

## Migration Path for Existing Users

If users already have accounts via web OAuth:
1. Their tokens are already in secrets.json ✓
2. Their Account records exist ✓
3. Everything continues to work ✓
4. New commands are just additional tools ✓

No breaking changes!
