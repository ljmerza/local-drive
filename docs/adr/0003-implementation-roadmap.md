# ADR 0003: Implementation Roadmap

* Status: Proposed
* Date: 2026-01-16

## Context

The core sync engine, Google Drive provider, content-addressed storage, and secrets management are functional. OAuth flow works end-to-end. However, several components remain incomplete or missing entirely, limiting production readiness.

This ADR documents the implementation priorities and decisions for completing the system.

## Current State

**Complete:**
- SyncEngine with initial/incremental sync and safe deletion state machine
- GoogleDriveClient with OAuth, changes API, file download, Docs export
- AccountStorage with CAS, atomic writes, materialization
- Secrets module for external token storage
- Django models with proper indexing
- `discover_accounts` and `sync_account` management commands
- OAuth callback flow with automatic account/SyncRoot creation
- Admin interface

**Incomplete:**
- PathBuilder lacks shared drive and multi-parent handling
- RetentionPolicy model exists but GC logic not implemented
- No web UI beyond admin and OAuth endpoints

**Missing:**
- Management commands: `add_account`, `verify_tokens`, `list_accounts`
- Status dashboard for monitoring account health
- Sync scheduling (Celery/Beat)
- OneDrive provider
- Restore/recovery interface
- Sync engine tests

## Decision

Implement in three phases, prioritizing operational visibility and reliability before expanding provider support.

### Phase 1: Operational Tooling

**Management Commands**

| Command | Purpose |
|---------|---------|
| `list_accounts` | Display account status table (provider, email, last sync, token expiry) |
| `verify_tokens` | Test token validity against provider APIs, report expiration |
| `add_account` | Create account record and initiate OAuth flow |

**GoogleDriveClient Enhancement**

Add `get_user_info()` method to retrieve authenticated user email/name. Required for connection testing and display purposes.

**Status Dashboard**

Template-based view at `/` showing:
- Account list with connection status (healthy/expiring/expired/error)
- Last sync timestamp and result per account
- Token expiration countdown
- Quick actions: sync now, re-authenticate

No JavaScript required. Status checks cached for 5 minutes.

### Phase 2: Retention and Scheduling

**Retention Enforcement**

Implement GC logic for RetentionPolicy:
- Purge versions exceeding `retention_days` and `retention_versions`
- Process quarantined items past retention window
- Management command: `run_gc [--dry-run]`
- Batch processing with configurable limits

**Celery Integration**

- Celery worker for async sync execution
- Beat scheduler for periodic syncs
- Per-account sync intervals (default: 6 hours)
- GC schedule (default: daily at 3am)

### Phase 3: Provider Expansion

**OneDrive Provider**

Implement `OneDriveClient` with:
- Microsoft Graph OAuth (v2.0 endpoint)
- Delta query for incremental sync
- Download and metadata APIs
- Proper error handling matching GoogleDriveClient patterns

**Shared Drive Support**

Extend GoogleDriveClient and PathBuilder for:
- Shared drive enumeration
- Per-shared-drive SyncRoot creation
- Correct path resolution within shared drives

## Test Coverage

Priority test additions:

1. **Sync engine tests** - State transitions, error recovery, batch processing
2. **OAuth callback tests** - Token storage, account creation, error handling
3. **Integration tests** - Full sync flow with mocked provider
4. **GC tests** - Retention policy enforcement

## Alternatives Considered

**1. Build OneDrive before operational tooling**
- Rejected: Cannot debug or monitor sync issues without visibility tools.

**2. Use Django-Q instead of Celery**
- Considered: Simpler setup, single-process option.
- Decision deferred: Celery is more widely documented; can revisit if complexity becomes problematic.

**3. React/Vue dashboard instead of templates**
- Rejected: Adds build complexity for minimal benefit. Template-based approach sufficient for status display.

## Consequences

### Positive

- Operational visibility before adding complexity
- Clear debugging path for sync issues
- Production-ready scheduling before multi-provider support
- Test coverage for critical paths

### Negative

- OneDrive users must wait for Phase 3
- Celery adds operational complexity (Redis/RabbitMQ dependency)
- No real-time sync status updates (polling only)

## Open Questions

1. **Celery broker**: Redis vs RabbitMQ? Redis is simpler for single-node deployments.
2. **GC batch size**: How many items per GC run to avoid long-running transactions?
3. **Sync failure alerting**: Email? Webhook? Defer to Phase 2 implementation.
