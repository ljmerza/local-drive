# Getting Google Drive Tokens with OAuth Playground

This guide shows you how to obtain Google Drive OAuth tokens without running the app's web UI. This is perfect for headless servers or network deployments where you can't use localhost.

## Why Use OAuth Playground?

- ✅ No need to access the app UI
- ✅ No redirect URI restrictions (no need for localhost or public domain)
- ✅ Perfect for headless servers (192.168.x.x, etc.)
- ✅ Direct browser-based authentication
- ✅ Get tokens in 5 minutes

## Prerequisites

1. Google Cloud Project with OAuth 2.0 credentials
   - If you don't have one, see "Setting Up Google Cloud Project" below
2. The email address of the Google account you want to back up

## Step-by-Step Guide

### 1. Go to OAuth 2.0 Playground

Open: https://developers.google.com/oauthplayground

### 2. Configure to Use Your OAuth Credentials

Click the **gear icon (⚙️)** in the top right corner.

In the configuration panel:
- ✅ Check **"Use your own OAuth credentials"**
- **OAuth Client ID**: Paste your client ID from Google Cloud Console
- **OAuth Client secret**: Paste your client secret
- Click **"Close"**

![OAuth Playground Configuration](https://i.imgur.com/example.png)

### 3. Select Google Drive Scopes

In the **left panel** under "Step 1: Select & authorize APIs":

1. Scroll down or search for **"Drive API v3"**
2. Expand it and select these scopes:
   - ✅ `https://www.googleapis.com/auth/drive.readonly`
   - ✅ `https://www.googleapis.com/auth/drive.metadata.readonly`

These scopes give read-only access to Google Drive files and metadata.

### 4. Authorize APIs

Click the blue **"Authorize APIs"** button at the bottom of the left panel.

You'll be redirected to Google's authorization page:
- Choose the Google account you want to back up
- Review the permissions
- Click **"Allow"**

You'll be redirected back to the OAuth Playground.

### 5. Exchange Authorization Code for Tokens

After authorization, you'll see an authorization code in the left panel.

Click **"Exchange authorization code for tokens"** (blue button in the left panel).

The right panel will show a JSON response like:

```json
{
  "access_token": "ya29.a0AfB_byDXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
  "expires_in": 3599,
  "refresh_token": "1//0gXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
  "scope": "https://www.googleapis.com/auth/drive.metadata.readonly https://www.googleapis.com/auth/drive.readonly",
  "token_type": "Bearer"
}
```

**IMPORTANT:** Copy both the `access_token` and `refresh_token` values. You'll need them in the next step.

⚠️ **Note:** The `refresh_token` only appears the **first time** you authorize. If you don't see it:
- Go to Google Account settings: https://myaccount.google.com/permissions
- Revoke access to your app
- Repeat steps 3-5

### 6. Add Tokens to secrets.json

On your server, edit `secrets.json`:

```json
{
  "oauth_clients": {
    "google": {
      "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
      "client_secret": "YOUR_CLIENT_SECRET",
      "redirect_uri": "http://localhost:8000/oauth/google/callback"
    }
  },
  "google:youremail@gmail.com": {
    "access_token": "ya29.a0AfB_byDXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "refresh_token": "1//0gXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    "expires_at": null
  }
}
```

**Replace:**
- `YOUR_CLIENT_ID` - Your OAuth client ID
- `YOUR_CLIENT_SECRET` - Your OAuth client secret
- `youremail@gmail.com` - The **exact email** you authorized with (must match!)
- `access_token` - The access_token from the playground
- `refresh_token` - The refresh_token from the playground

**Save the file** and set proper permissions:
```bash
chmod 600 secrets.json
```

### 7. Start the App (Auto-Discovery)

The app automatically discovers and creates accounts from secrets.json on startup!

**Start the app:**

```bash
docker-compose up -d
```

**Check the logs:**

```bash
docker logs local-drive-app
```

You should see:
```
Auto-discovering accounts from secrets file...
Auto-discovery: Created 1 new account(s)
  ✓ google_drive / youremail@gmail.com
```

**That's it!** The account is automatically created and ready to sync.

**Manual Discovery (Optional)**

If you add more tokens later without restarting:

```bash
docker exec local-drive-app python manage.py discover_accounts
```

Output:
```
Found 1 account(s) in secrets.json

✓ Created 1 new account(s):
  • google_drive / youremail@gmail.com

✓ Accounts are ready to sync!

Run sync with:
  python manage.py sync_account 1  # youremail@gmail.com
```

### 8. Test the Sync

Run a sync to verify everything works:

```bash
docker exec local-drive-app python manage.py sync_account 1
```

Replace `1` with your account ID if different.

You should see output like:
```
Starting sync for account: My Google Drive Backup (youremail@gmail.com)
Syncing root: My Drive
Found 150 files to sync
Downloading files...
✓ Sync completed successfully
```

### 9. Set Up Automated Syncing (Optional)

Add a cron job to sync regularly:

```bash
# Edit crontab
crontab -e

# Add this line to sync every 6 hours
0 */6 * * * docker exec local-drive-app python manage.py sync_account 1
```

## Troubleshooting

### "No refresh_token in response"

The refresh token only appears the first time you authorize. To fix:
1. Go to https://myaccount.google.com/permissions
2. Find your app and revoke access
3. Repeat the OAuth Playground steps

### "TokenNotFoundError" or "No tokens for account"

- Check that the email in `secrets.json` **exactly matches** the email in the Account record
- Format must be: `google:youremail@gmail.com` in secrets.json
- Account.email must be: `youremail@gmail.com`

### "Invalid grant" or "Token expired"

- The access token expires after 1 hour (this is normal)
- The app automatically refreshes it using the refresh_token
- Make sure your `refresh_token` is correct in secrets.json

### "Insufficient permissions"

- Make sure you selected both scopes in OAuth Playground:
  - `drive.readonly`
  - `drive.metadata.readonly`

---

## Setting Up Google Cloud Project (If Needed)

If you don't have OAuth credentials yet:

### 1. Create a Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "Select a project" → "New Project"
3. Name it (e.g., "Local Drive Backup")
4. Click "Create"

### 2. Enable Google Drive API

1. Go to "APIs & Services" → "Library"
2. Search for "Google Drive API"
3. Click on it and click "Enable"

### 3. Configure OAuth Consent Screen

1. Go to "APIs & Services" → "OAuth consent screen"
2. Choose "External" (unless you have Google Workspace)
3. Click "Create"
4. Fill in:
   - **App name**: Local Drive Backup
   - **User support email**: Your email
   - **Developer contact**: Your email
5. Click "Save and Continue"
6. Click "Add or Remove Scopes"
7. Add these scopes:
   - `drive.readonly`
   - `drive.metadata.readonly`
8. Click "Update" → "Save and Continue"
9. Add your email as a test user
10. Click "Save and Continue"

### 4. Create OAuth Credentials

1. Go to "APIs & Services" → "Credentials"
2. Click "Create Credentials" → "OAuth 2.0 Client ID"
3. Application type: **"Web application"**
4. Name: "Local Drive App"
5. Authorized redirect URIs:
   - Add: `https://developers.google.com/oauthplayground` (for OAuth Playground)
   - Add: `http://localhost:8000/oauth/google/callback` (for local testing)
6. Click "Create"
7. **Copy the Client ID and Client Secret** - you'll need these!

Now you can use these credentials in the OAuth Playground!

---

## Summary

1. ✅ Set up Google Cloud Project (one-time)
2. ✅ Use OAuth Playground to get tokens (5 minutes)
3. ✅ Add tokens to `secrets.json`
4. ✅ Start the app (auto-discovers and creates accounts)
5. ✅ Run syncs from your server at any IP address

**That's it!** No manual account creation, no localhost needed, no redirect URI issues. Perfect for headless servers! 🎉
