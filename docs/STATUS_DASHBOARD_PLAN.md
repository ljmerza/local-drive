# Status Dashboard UI Plan

## Overview

Create a simple read-only status dashboard showing OAuth connection health and token status for all accounts.

## Goals

- ✅ Quick view of all accounts and their connection status
- ✅ Token health (expired, expiring soon, valid)
- ✅ Time until token refresh needed
- ✅ Last successful API connection test
- ✅ Clear error messages if tokens are invalid
- ❌ NO sync triggering (just status display)
- ❌ NO file browsing (use admin for that)

## URL Structure

```
/                          # Status Dashboard (new home page)
/admin/                    # Existing Django admin (unchanged)
/oauth/google/             # Existing OAuth flow (unchanged)
/oauth/google/callback/    # Existing OAuth callback (unchanged)
```

## Page Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Local Drive Backup - Status Dashboard                      │
│  Last updated: 2026-01-06 15:30:45                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  📊 System Overview                                          │
│  ┌──────────────┬──────────────┬──────────────────────┐     │
│  │ Accounts     │ Active Syncs │ Last Sync            │     │
│  │ 3            │ 2            │ 2 minutes ago        │     │
│  └──────────────┴──────────────┴──────────────────────┘     │
│                                                              │
│  🔐 Account Connection Status                                │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ ✅ Google Drive - user@gmail.com                       │ │
│  │    Status: Connected                                   │ │
│  │    Access Token: Valid (expires in 45 minutes)         │ │
│  │    Refresh Token: Available                            │ │
│  │    Last Verified: 2 minutes ago                        │ │
│  │    Last Sync: 2 minutes ago                            │ │
│  │    [View in Admin]                                     │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ ⚠️  Google Drive - work@company.com                    │ │
│  │    Status: Token Expiring Soon                         │ │
│  │    Access Token: Expires in 5 minutes                  │ │
│  │    Refresh Token: Will auto-refresh on next sync       │ │
│  │    Last Verified: 1 hour ago                           │ │
│  │    Last Sync: 6 hours ago                              │ │
│  │    [View in Admin]                                     │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ ❌ Google Drive - broken@gmail.com                     │ │
│  │    Status: Connection Failed                           │ │
│  │    Access Token: Expired                               │ │
│  │    Refresh Token: Invalid or revoked                   │ │
│  │    Error: Invalid grant (401)                          │ │
│  │    Last Verified: Just now                             │ │
│  │    Last Sync: Never                                    │ │
│  │    ⚠️  Action Required: Re-authenticate via OAuth      │ │
│  │    [Start OAuth Flow] [View in Admin]                  │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ 🔄 Google Drive - new@gmail.com                        │ │
│  │    Status: Tokens Found, Not Yet Verified              │ │
│  │    Access Token: Present (not yet tested)              │ │
│  │    Refresh Token: Present                              │ │
│  │    Last Verified: Never                                │ │
│  │    Last Sync: Never                                    │ │
│  │    ℹ️  Run first sync to verify connection             │ │
│  │    [View in Admin]                                     │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  📝 Quick Links                                              │
│  • Django Admin Panel                                       │
│  • View Sync Sessions                                       │
│  • View Backup Items                                        │
│  • Add New Account (OAuth)                                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Data Model

### AccountStatus (computed, not stored)

For each account, compute:

```python
{
    'account': Account object,
    'has_tokens': bool,
    'token_status': 'valid' | 'expired' | 'expiring_soon' | 'missing' | 'unknown',
    'access_token_expires_at': datetime | None,
    'time_until_expiry': timedelta | None,  # Human readable: "45 minutes", "2 hours"
    'refresh_token_available': bool,
    'connection_status': 'connected' | 'token_expired' | 'token_invalid' | 'not_verified' | 'connection_error',
    'last_verified_at': datetime | None,  # From cache or last API test
    'last_sync_at': datetime | None,  # From SyncRoot
    'error_message': str | None,
    'needs_action': bool,  # True if user needs to re-auth
}
```

## Status Determination Logic

### 1. Token Status

```python
def get_token_status(account):
    """Determine token health status."""

    # Check if tokens exist in secrets.json
    if not secrets.has_tokens(account):
        return 'missing'

    tokens = secrets.get_tokens(account)

    # No access token
    if not tokens.get('access_token'):
        return 'missing'

    # Check expiry
    expires_at = tokens.get('expires_at')
    if not expires_at:
        return 'unknown'  # No expiry info, assume valid

    now = timezone.now()
    time_left = expires_at - now

    if time_left.total_seconds() < 0:
        return 'expired'
    elif time_left.total_seconds() < 600:  # < 10 minutes
        return 'expiring_soon'
    else:
        return 'valid'
```

### 2. Connection Status

```python
def get_connection_status(account):
    """Test actual API connectivity."""

    # Check tokens exist
    if not secrets.has_tokens(account):
        return {
            'status': 'not_verified',
            'error': 'No tokens in secrets.json'
        }

    # Try to create client and test connection
    try:
        client = GoogleDriveClient(account)

        # Make lightweight API call (get user info)
        user_info = client.get_user_info()  # Need to implement this

        return {
            'status': 'connected',
            'verified_at': timezone.now(),
            'error': None
        }

    except TokenExpiredError:
        return {
            'status': 'token_expired',
            'error': 'Access token expired (will auto-refresh on next sync)'
        }

    except InvalidGrantError:
        return {
            'status': 'token_invalid',
            'error': 'Refresh token invalid or revoked. Re-authentication required.'
        }

    except Exception as e:
        return {
            'status': 'connection_error',
            'error': str(e)
        }
```

### 3. Caching Connection Tests

Don't want to hit API on every page load:

```python
# Cache connection status for 5 minutes
from django.core.cache import cache

def get_cached_connection_status(account):
    """Get connection status with caching."""
    cache_key = f'connection_status_{account.id}'

    cached = cache.get(cache_key)
    if cached:
        return cached

    # Test connection
    status = get_connection_status(account)

    # Cache for 5 minutes
    cache.set(cache_key, status, 300)

    return status
```

## Implementation Files

### 1. View: `backup/views/dashboard.py`

```python
"""Status dashboard view."""

from django.shortcuts import render
from django.utils import timezone
from datetime import timedelta

from backup.models import Account
from backup import secrets


def status_dashboard(request):
    """Show status dashboard with account connection info."""

    accounts = Account.objects.all().prefetch_related('sync_roots')

    account_statuses = []
    for account in accounts:
        status = get_account_status(account)
        account_statuses.append(status)

    # Sort: errors first, then by last_sync
    account_statuses.sort(
        key=lambda x: (
            x['needs_action'],  # Errors first
            x['last_sync_at'] or timezone.now() - timedelta(days=999)  # Then by recency
        ),
        reverse=True
    )

    context = {
        'account_statuses': account_statuses,
        'total_accounts': len(account_statuses),
        'active_accounts': sum(1 for s in account_statuses if s['connection_status'] == 'connected'),
        'last_updated': timezone.now(),
    }

    return render(request, 'backup/dashboard.html', context)


def get_account_status(account):
    """Get comprehensive status for an account."""
    # Implementation here
    pass
```

### 2. Template: `backup/templates/backup/dashboard.html`

Simple, clean HTML with minimal CSS (no framework dependencies).

```html
<!DOCTYPE html>
<html>
<head>
    <title>Local Drive Backup - Status</title>
    <style>
        /* Minimal, clean CSS */
        body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { background: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        .overview { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 15px; border-radius: 8px; }
        .account-card { background: white; padding: 20px; border-radius: 8px; margin-bottom: 15px; border-left: 4px solid #ccc; }
        .account-card.connected { border-left-color: #4caf50; }
        .account-card.warning { border-left-color: #ff9800; }
        .account-card.error { border-left-color: #f44336; }
        .account-card.unknown { border-left-color: #2196f3; }
        /* More styles... */
    </style>
</head>
<body>
    <div class="container">
        <!-- Dashboard content -->
    </div>
</body>
</html>
```

### 3. URL: Update `backup/urls.py`

```python
urlpatterns = [
    path("", views.status_dashboard, name="dashboard"),  # NEW: Home page
    path("oauth/google/", views.google_auth_start, name="google_auth_start"),
    path("oauth/google/callback/", views.google_auth_callback, name="google_auth_callback"),
]
```

### 4. Helper: `backup/connection_test.py`

```python
"""Connection testing utilities."""

from django.core.cache import cache
from django.utils import timezone

from backup.providers.google_drive import GoogleDriveClient


def test_account_connection(account):
    """Test if account can connect to provider API."""

    # Check cache first (5 min TTL)
    cache_key = f'connection_test_{account.id}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Test connection
    result = _test_connection(account)

    # Cache result
    cache.set(cache_key, result, 300)

    return result


def _test_connection(account):
    """Actually test the connection."""
    # Implementation
    pass
```

## Feature Checklist

### Phase 1: Basic Status Display (MVP)
- [ ] Show list of all accounts
- [ ] Display token status (valid/expired/missing)
- [ ] Show time until token expiry
- [ ] Show last sync time
- [ ] Color-coded status indicators (green/yellow/red)
- [ ] Simple HTML template (no JavaScript)

### Phase 2: Connection Testing
- [ ] Implement lightweight API test call
- [ ] Cache test results (5 min TTL)
- [ ] Show connection errors
- [ ] Display "Last Verified" timestamp

### Phase 3: Polish
- [ ] Auto-refresh page every 30 seconds (meta refresh)
- [ ] Add quick links to admin sections
- [ ] Show system overview stats
- [ ] Responsive design for mobile

### Phase 4: Future Enhancements (Optional)
- [ ] Manual "Test Connection" button (AJAX)
- [ ] Real-time updates (WebSocket/SSE)
- [ ] Token refresh button
- [ ] Charts/graphs

## Technical Requirements

### Dependencies
- ✅ Django templates (built-in)
- ✅ Django cache framework (default in-memory cache is fine)
- ❌ No JavaScript required for MVP
- ❌ No external CSS frameworks

### API Methods Needed

Need to add to `GoogleDriveClient`:

```python
def get_user_info(self) -> dict:
    """
    Get basic user info (lightweight API call for connection testing).

    Returns:
        {
            'email': str,
            'display_name': str,
            'storage_quota': dict
        }
    """
    # Implementation using Drive API about endpoint
    pass
```

### Performance Considerations

- **Cache API tests** - Don't hit Google API on every page load
- **Batch queries** - Use `select_related()` and `prefetch_related()`
- **Lightweight template** - No heavy CSS/JS frameworks
- **Auto-refresh** - Use simple meta refresh, not AJAX polling

## Error States to Handle

1. **No tokens in secrets.json** - Show "Not configured" message
2. **Access token expired** - Show "Will refresh on next sync" (normal)
3. **Refresh token invalid** - Show "Re-authentication required" with OAuth link
4. **Network error** - Show "Connection test failed" with error message
5. **Account exists but no tokens** - Show "Tokens missing from secrets.json"

## Success Criteria

✅ User can see at a glance which accounts are healthy
✅ User can identify which accounts need attention
✅ User understands when tokens will expire
✅ User knows when last sync happened
✅ Page loads quickly (< 1 second)
✅ No external dependencies
✅ Mobile-friendly

## Implementation Estimate

- **Phase 1 (MVP)**: 2-3 hours
  - View logic: 1 hour
  - Template: 1 hour
  - Testing: 30 min

- **Phase 2 (Connection testing)**: 1-2 hours
  - API test method: 30 min
  - Caching: 30 min
  - Error handling: 30 min

- **Phase 3 (Polish)**: 1 hour
  - CSS refinement: 30 min
  - Quick links: 15 min
  - Stats: 15 min

**Total: 4-6 hours for full implementation**

## Sample Use Cases

### Use Case 1: Healthy System
```
User opens http://192.168.1.76:8000/
Sees: "✅ All 3 accounts connected"
All accounts show green status
Can see last sync was 10 minutes ago
```

### Use Case 2: Token About to Expire
```
User opens dashboard
Sees: "⚠️ 1 account needs attention"
One account shows yellow with "Token expires in 8 minutes"
Message: "Will auto-refresh on next sync"
No action needed
```

### Use Case 3: Broken Token
```
User opens dashboard
Sees: "❌ 1 account has errors"
Account shows red with "Connection Failed"
Error: "Invalid grant - refresh token revoked"
Button: "Start OAuth Flow" to re-authenticate
```

### Use Case 4: New Account Not Yet Tested
```
User just added tokens to secrets.json
User restarts app (auto-discovers account)
User opens dashboard
Sees: "🔄 New account detected"
Message: "Not yet verified - run first sync"
```

## Next Steps

1. Review this plan
2. Decide on which phases to implement
3. Start with Phase 1 (MVP) - basic status display
4. Iterate based on feedback

---

**Question for you:** Should I implement Phase 1 (MVP) now, or would you like to adjust the plan first?
