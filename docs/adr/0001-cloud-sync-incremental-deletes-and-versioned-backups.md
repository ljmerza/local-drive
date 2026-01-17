# ADR 0001: Cloud Backup With Incremental Deletes via Versioned Backups

* Status: Proposed
* Date: 2026-01-04

## Context

We want an app (Django backend + simple UI via Django templates) that backs up cloud services (Google Drive, OneDrive, etc.) to local storage. Key requirements:

- Support multiple providers and multiple accounts per provider.
- Incrementally capture additions/updates (new files and changed files) efficiently.
- Handle deletions safely, especially when the app cannot reliably distinguish:
  - A user-initiated deletion upstream (should eventually be removed locally), vs.
  - A transient “missing” condition (API inconsistency, permission changes, temporary provider outage, partial sync, or a missed change token event).

As a backup product, the primary failure mode we must avoid is: a file disappears upstream due to a transient condition (or we miss a deletion event), and the next run discards the last known local copy. We should preserve recoverability within a user-configured retention window.

## Problem Statement: “Incremental Deletes”

Many provider APIs offer change feeds (delta endpoints / change tokens) that include explicit delete events (“tombstones”). In practice, we may still see “missing” files without a clear tombstone due to:

- Change-feed gaps (token invalidation, missed polling windows, webhook delivery issues, client bugs).
- Permission/ownership changes (file removed from a shared folder; account loses access).
- Listing inconsistencies (eventual consistency, transient API errors).
- Partial sync (user pauses, process crashes, rate limiting).

If our local folder is treated as a strict mirror, a single ambiguous “missing” observation could delete user data.

## Decision Drivers

- Data safety over strict mirroring.
- Clear user expectations and auditability (“what happened to my file?”).
- Provider-agnostic design; differing semantics across Google Drive and OneDrive.
- Efficient incremental sync (avoid full listings whenever possible).
- Multiple accounts and multiple roots (e.g., “My Drive”, shared drives, OneDrive personal vs business).

## Decision

We will implement *backup-first semantics*:

1. **Ingest layer** pulls provider changes into local storage (current backup set, read-only).
2. **Versioning layer** maintains a *versioned history* with configurable retention.

Deletions from providers are recorded, but local data is only removed via retention/GC (or explicit user action). The default posture is to keep data.

### Core Approach

**A. Track stable provider identifiers and state**

- Store provider-specific stable IDs (`provider_item_id`) rather than relying on paths alone.
- Persist a local metadata DB (Django models) with:
  - Account, provider, root scopes.
  - Items: IDs, current known path, type, hashes/etag, modified times, size, parents, permissions summary.
  - Sync cursors: change tokens / delta links / checkpoint timestamps.
  - Observations: last-seen-at, last-successful-sync-at.

**B. Prefer explicit change feeds, treat “missing” as ambiguous**

- Use change feeds (Google Drive Changes API, Microsoft Graph delta queries) as the primary incremental mechanism.
- If a change feed returns an explicit tombstone/delete marker: mark the item as **DeletedUpstream**.
- If an item is absent from a listing without an explicit tombstone, mark as **MissingUpstream** (ambiguous).

**C. Implement an “archive + retention” deletion workflow**

When the sync engine observes a deletion upstream (or suspects one), we should not destroy local content immediately. Instead:

- Mark the item state (`DeletedUpstream` or `MissingUpstream`) and optionally move the “current” copy out of the active backup view into a managed **archive/quarantine area**.
- Create/ensure a corresponding **backup version record** exists so the content can be recovered.
- Remove local data only when retention rules allow (and only from the archive/quarantine area).

Recommended confirmation options (configurable; may vary by provider):

- **Time-based**: Only purge after `retention_days` and/or `retention_versions` policy is satisfied.
- **Sync-count-based**: Require the item to be “DeletedUpstream” or “MissingUpstream” for `N` consecutive successful syncs before purging “current” copies (versions remain until retention allows).
- **Second-probe verification**: For “MissingUpstream”, attempt a targeted lookup by stable ID (where API supports it) before acting.
- **User review**: Expose “deleted/missing upstream” items in the UI with restore/export controls (and optional “purge now”).

**D. Versioned backups are incremental and bounded**

The system keeps a bounded history per item:

- Store versions with metadata (timestamp, source sync run, provider revision/etag, checksum).
- Retain `X` versions and/or versions younger than `Y` days, user-configurable per account/root.
- Storage location can be:
  - Local filesystem (content-addressed store), or
  - A pluggable backend (S3, local disk, etc.) depending on product goals.

This makes upstream deletes safe: even if we later determine a deletion was legitimate, the user can restore/export from a prior version within the configured window.

## Architecture Notes (Django + Templates)

- **Django** hosts:
  - Provider connectors (OAuth, token refresh, API wrappers).
  - Sync orchestration (Celery tasks + beat scheduling per account/root).
  - Persistent metadata DB and backup index.
  - UI views (sync status, conflicts, pending deletes, retention settings) rendered with Django templates.
  - Optional minimal JavaScript for polling status and inline actions (export/restore, purge), avoiding a SPA.

If this is intended to run on a user’s machine, the Django server effectively acts as a local daemon with a web UI. If centralized, then local filesystem access requires an agent component; that is a separate ADR.

## Alternatives Considered

1. **Strict mirroring (delete locally immediately)**
   - Pros: simple mental model, minimal storage usage.
   - Cons: not a backup product; ambiguous “missing” can cause irreversible loss.

2. **Never delete locally (unbounded)**
   - Pros: simplest “backup” posture.
   - Cons: storage growth unbounded; eventually becomes unusable without retention/GC.

3. **Provider-native trash only**
   - Pros: leverage upstream recycle bin semantics.
   - Cons: does not protect against local-side errors; retention depends on provider; doesn’t address “missing” that isn’t a deletion.

4. **Full snapshot backups (periodic complete copies)**
   - Pros: simplest correctness story.
   - Cons: expensive (time + storage), not scalable for large drives.

## Consequences

### Positive

- Prevents accidental data loss due to transient upstream states or missed change events.
- Provides a coherent user story: “sync to working folder + version history to recover”.
- Auditable: we can show “why” a file was removed (tombstone, missing, user-confirmed).

### Negative / Costs

- Requires local metadata DB and careful state machine logic.
- Uses additional storage for archive/quarantine and versions.
- More complex UI/UX (retention settings, restore flows, pending delete review).

## Key Problems to Solve (Non-Exhaustive)

- **Identity & moves**: map stable IDs to local paths; handle renames/moves without creating duplicates.
- **Shared/permissioned content**: “missing” may mean access revoked; treat differently from deletion.
- **User-modified local copies**: decide whether the backup folder is read-only (recommended) or whether to detect/ignore local edits.
- **Change token invalidation**: provider may require resync; ensure resync does not trigger mass deletions.
- **Partial failures**: if a sync run fails mid-way, don’t treat unvisited items as missing.
- **Scale & rate limits**: batching, backoff, checkpointing, resumable downloads.
- **Local filesystem semantics**: filename normalization, forbidden characters, path length limits, case sensitivity.
- **Security**: token storage, encryption at rest for metadata and backups, least-privilege scopes.
- **Multi-account separation**: prevent path collisions and ID collisions; per-account namespaces.
- **Observability**: per-run logs, counters (created/updated/deleted/quarantined), alerting for anomalies.

## Proposed State Machine (Sketch)

Per item, maintain a state:

- `Active`: present upstream and in working copy.
- `DeletedUpstream`: explicit tombstone received.
- `MissingUpstream`: disappeared without explicit deletion (ambiguous).
- `Quarantined`: removed from working copy but retained locally.
- `Purged`: removed from quarantine after retention/confirmation.

Transitions must be tied to successful, complete sync runs and explicit evidence (tombstone or repeated absence).

## Open Questions

The following choices are fixed for this project:

1. **Backup set is read-only**
   - Treat the active backup view as read-only.
   - Provide “Export/Restore” actions to a user-chosen location (including an optional “restore into backup view” for convenience).

2. **Data is partitioned per provider → per account**
   - Local storage is organized by provider and account to avoid collisions and make deletion/export predictable.
   - Retention policies and storage caps apply at least at the account level (with optional per-root overrides).

3. **Upstream deletion semantics**
   - Track `Active`, `DeletedUpstream` (explicit tombstone), and `MissingUpstream` (ambiguous).
   - Default UI: hide `DeletedUpstream` from the active backup view, but keep it visible in an audit/history view.
   - Never destroy version history until retention/GC allows (or user explicitly purges).

4. **Default safety settings (safe-by-default)**
   - Default retention: keep versions for **30 days** and keep at least **10 versions** per item (whichever retains more).
   - Default missing handling: require **2 consecutive successful sync runs** observing `MissingUpstream` before moving the “current” copy into archive/quarantine.
   - Default purge behavior: never purge from archive/quarantine until retention permits; purge is always reversible within the retention window via restore/export.

5. **Scopes are explicit and opt-in**
   - Model provider scopes/roots explicitly (e.g., “My Drive”, shared drives).
   - Default: only primary personal scope is enabled; shared content is opt-in.

6. **Rebuild/reconcile mode is required**
   - Provide an administrative “reconcile” flow that can rebuild the DB index from local storage and then re-scan the provider.
   - Reconcile runs must never infer deletions from absence alone.

7. **Communicating uncertainty**
   - UI and logs must clearly distinguish “confirmed deleted (tombstone)” vs “missing (uncertain)”.
   - Expose evidence: tombstone event vs repeated absence + last-seen time.

8. **Cursor invalidation safety**
   - If a provider cursor/delta token is invalidated, perform a full scan that only adds/updates.
   - Do not move items into `MissingUpstream` or archive/quarantine based on absence during the resync scan.

Remaining open questions (to be decided during implementation):

- Archive/quarantine will be a physical folder move within the account’s backup root (keeping the active backup tree clean while preserving recoverability).
- A global storage cap across all accounts is deferred; start with per-account retention/GC and add a global cap only if needed.
