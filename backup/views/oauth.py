"""
OAuth views for Google Drive authentication.
"""

import logging
import secrets as stdlib_secrets

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_GET

from backup import secrets
from backup.models import Account, Provider, SyncRoot
from backup.providers.google_drive import (
    GoogleDriveClient,
    exchange_code_for_tokens,
    get_authorization_url,
)

logger = logging.getLogger(__name__)


@require_GET
def google_auth_start(request: HttpRequest) -> HttpResponse:
    """
    Start the Google OAuth flow.

    Generates authorization URL and redirects user to Google.
    Stores state in session for CSRF protection.
    """
    # Generate state for CSRF protection
    state = stdlib_secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state

    authorization_url, _ = get_authorization_url(state=state)

    logger.info("Starting Google OAuth flow")
    return redirect(authorization_url)


@require_GET
def google_auth_callback(request: HttpRequest) -> HttpResponse:
    """
    Handle the Google OAuth callback.

    Exchanges authorization code for tokens, creates or updates
    the Account, and sets up the default SyncRoot.
    Tokens are stored in the secrets file, not the database.
    """
    # Check for errors from Google
    error = request.GET.get("error")
    if error:
        logger.error(f"Google OAuth error: {error}")
        return HttpResponseBadRequest(f"OAuth error: {error}")

    # Verify state for CSRF protection
    state = request.GET.get("state")
    stored_state = request.session.pop("google_oauth_state", None)
    if not state or state != stored_state:
        logger.warning("OAuth state mismatch - possible CSRF attack")
        return HttpResponseBadRequest("Invalid state parameter")

    # Get authorization code
    code = request.GET.get("code")
    if not code:
        return HttpResponseBadRequest("Missing authorization code")

    try:
        # Exchange code for tokens
        token_data = exchange_code_for_tokens(code)

        # Create or update account (no tokens in DB)
        account, created = Account.objects.update_or_create(
            provider=Provider.GOOGLE_DRIVE,
            email=token_data["email"],
            defaults={
                "name": token_data["name"],
                "is_active": True,
            },
        )

        # Store tokens in secrets file
        secrets.set_tokens(
            account,
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=token_data["expires_at"],
        )

        if created:
            logger.info(f"Created new Google Drive account: {account.email}")

            # Create default sync root for "My Drive"
            client = GoogleDriveClient(account)
            start_token = client.get_start_page_token()

            SyncRoot.objects.create(
                account=account,
                provider_root_id="root",
                name="My Drive",
                sync_cursor=start_token,
                is_enabled=True,
            )
            logger.info(f"Created default sync root for account {account.id}")
        else:
            logger.info(f"Updated existing Google Drive account: {account.email}")

        # Redirect to success page (admin for now)
        return redirect("/admin/backup/account/")

    except Exception as e:
        logger.exception(f"OAuth callback error: {e}")
        return HttpResponseBadRequest(f"Authentication failed: {e}")
