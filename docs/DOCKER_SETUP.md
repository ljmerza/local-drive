# Docker Deployment Guide

This guide explains how to deploy the Local Drive backup application using Docker.

## New Simplified Architecture

All sensitive credentials (OAuth client secrets AND user tokens) are now stored in a single `secrets.json` file. This eliminates the need for multiple environment variables and simplifies deployment.

## Quick Start (Headless Server / Network Deployment)

**For servers at 192.168.x.x or headless setups, use OAuth Playground to get tokens manually.**

### 1. Get OAuth Tokens

See **[OAUTH_PLAYGROUND_GUIDE.md](OAUTH_PLAYGROUND_GUIDE.md)** for complete instructions.

Quick summary:
1. Get Google OAuth client credentials from Cloud Console
2. Use [OAuth Playground](https://developers.google.com/oauthplayground) to get access + refresh tokens
3. Add both to `secrets.json`

Example `secrets.json`:
```json
{
  "oauth_clients": {
    "google": {
      "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
      "client_secret": "YOUR_CLIENT_SECRET",
      "redirect_uri": "http://localhost:8000/oauth/google/callback"
    }
  },
  "google:yourname@gmail.com": {
    "access_token": "ya29.a0AfB_...",
    "refresh_token": "1//0gXXX...",
    "expires_at": null
  }
}
```

### 2. Start the App

```bash
docker-compose up -d
```

**The app automatically discovers accounts from secrets.json!**

Check logs to verify:
```bash
docker logs local-drive-app
# You should see:
# Auto-discovery: Created 1 new account(s)
#   ✓ google_drive / yourname@gmail.com
```

### 3. Run Syncs

```bash
# List accounts
docker exec local-drive-app python manage.py discover_accounts

# Sync account
docker exec local-drive-app python manage.py sync_account 1
```

That's it! No browser needed, works from any IP address.

---

## Alternative: Web UI OAuth Flow

If you prefer using the web UI (only works with localhost):

### 1. Create secrets.json with OAuth client credentials

```bash
cp secrets.json.example secrets.json
```

Add only the OAuth client section (tokens will be added automatically):
```json
{
  "oauth_clients": {
    "google": {
      "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
      "client_secret": "YOUR_CLIENT_SECRET",
      "redirect_uri": "http://localhost:8000/oauth/google/callback"
    }
  }
}
```

### 2. Start the app

```bash
docker-compose up -d
```

### 3. Authorize via browser

Visit http://localhost:8000/oauth/google/ and authorize. Tokens are saved automatically to `secrets.json`.

### 4. Account is auto-created

The app auto-discovers the account from the newly added tokens. Ready to sync!

---

## Configuration

### (Optional) Create .env file

The .env file is optional and mainly for customizing Django settings:

```bash
cp .env.example .env
```

Default values work fine for local development.

### 3. Build and Run

```bash
docker-compose up --build
```

The application will:
- Run database migrations automatically
- Create a default admin user (username: `admin`, password: `admin`)
- Start on port 8000

### 4. Access the Application

- **Web UI**: http://localhost:8000
- **Admin Panel**: http://localhost:8000/admin
- **Start OAuth**: http://localhost:8000/oauth/google/

## File Structure

```
.
├── secrets.json          # OAuth credentials + user tokens (git-ignored)
├── db.sqlite3           # Database (auto-created)
├── backup_data/         # Backup storage (auto-created)
├── .env                 # Optional Django settings
└── docker-compose.yml   # Docker orchestration
```

## How It Works

### Secrets File Structure

The `secrets.json` file has two sections:

```json
{
  "oauth_clients": {
    "google": {
      "client_id": "...",
      "client_secret": "...",
      "redirect_uri": "..."
    }
  },
  "google:user@example.com": {
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": "..."
  }
}
```

- **oauth_clients**: Your application's OAuth credentials (you create these)
- **google:user@example.com**: User tokens (generated automatically during OAuth flow)

### First Run

**Option 1: Manual Tokens (Recommended for headless servers)**

1. Get OAuth tokens from [OAuth Playground](https://developers.google.com/oauthplayground) (see OAUTH_PLAYGROUND_GUIDE.md)
2. Add tokens to `secrets.json`
3. Start the app - it **automatically discovers and creates accounts**
4. Run syncs from anywhere (no browser needed)

**Option 2: Web UI OAuth Flow**

1. You provide OAuth client credentials in `secrets.json`
2. User visits `/oauth/google/` and authorizes
3. Google redirects back with access/refresh tokens
4. App automatically saves tokens to `secrets.json` under the user's account key
5. App can now sync Google Drive data

## Network Access (Optional)

If you want to access the app from other devices on your network:

### Option 1: Use localhost only (Recommended)

Keep `redirect_uri: "http://localhost:8000/oauth/google/callback"` and only access from the host machine.

### Option 2: Use a local domain (Workaround for Google's restrictions)

Google OAuth doesn't allow private IP addresses (like 192.168.x.x) as redirect URIs. You need to use a domain name:

1. **Choose a local domain**: e.g., `localdrive.local`

2. **Add to /etc/hosts on each device**:
   ```bash
   # On Linux/Mac: /etc/hosts
   # On Windows: C:\Windows\System32\drivers\etc\hosts
   192.168.1.76    localdrive.local
   ```

3. **Update secrets.json**:
   ```json
   {
     "oauth_clients": {
       "google": {
         "redirect_uri": "http://localdrive.local:8000/oauth/google/callback"
       }
     }
   }
   ```

4. **Update .env**:
   ```bash
   ALLOWED_HOSTS=localhost,127.0.0.1,localdrive.local
   ```

5. **Update Google Cloud Console**:
   - Add `http://localdrive.local:8000/oauth/google/callback` as an authorized redirect URI

6. **Access from any device**: http://localdrive.local:8000

## Security Notes

- `secrets.json` has 600 permissions (owner read/write only)
- File is git-ignored by default
- OAuth tokens are NEVER stored in the database
- Consider placing on an encrypted volume for production

## Troubleshooting

### "Invalid redirect_uri" error from Google
- Make sure the redirect_uri in secrets.json EXACTLY matches what's in Google Cloud Console
- Google doesn't allow IP addresses - use localhost or a domain name

### Can't access from other devices
- Check that ALLOWED_HOSTS includes your domain/IP
- Verify /etc/hosts is set up on the client device
- Ensure port 8000 is not blocked by firewall

### OAuth not working
- Verify secrets.json has correct client_id and client_secret
- Check that Google Cloud Console has the OAuth consent screen configured
- Ensure the redirect URI is authorized in Google Cloud Console

## Production Considerations

For production deployment:

1. **Use HTTPS**: Google requires HTTPS for non-localhost redirect URIs
2. **Use a real domain**: Register a domain and set up SSL
3. **Change admin password**: Default is `admin`/`admin`
4. **Remove source mount**: Comment out the `.:/app` volume in docker-compose.yml
5. **Use PostgreSQL**: Uncomment the PostgreSQL service in docker-compose.yml
6. **Set DEBUG=False**: Update config/settings.py
7. **Use production WSGI server**: Switch from runserver to gunicorn
