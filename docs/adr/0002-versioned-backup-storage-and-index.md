# ADR 0002: Versioned Backup Storage and Version Tracking

* Status: Proposed
* Date: 2026-01-04
* Related: ADR 0001 (incremental deletes + quarantine)

## Context

ADR 0001 proposes using versioned backups and an archive/quarantine step to safely handle ambiguous deletions during cloud→local backup. This ADR defines how file versions are stored and tracked so we can:

- Restore prior versions reliably.
- Deduplicate data to control storage growth.
- Enforce retention policies (`keep_last_n` and/or `keep_days`).
- Garbage collect unreferenced data safely.
- Keep the design provider-agnostic and resilient to sync restarts.

## Decision Drivers

- Fast restores/exports for a single file and for whole folders.
- Bounded storage with predictable retention behavior.
- Minimal complexity for a “local daemon” deployment.
- Integrity and auditability (detect corruption; explain why a version exists).
- Works across multiple accounts/providers without collisions.

## Decision

We will implement **content-addressed storage (CAS)** for file contents plus a **relational index** of versions in the Django DB.

- Content is stored as immutable blobs addressed by cryptographic digest (e.g., `sha256`).
- Each file version is a DB record pointing to one blob digest (or an optional chunk manifest in the future).
- File identity uses the provider’s stable item ID (`provider_item_id`) from the sync layer, and can additionally track the local path at time of version creation.

Data is partitioned per **provider → account** for predictability and isolation. This means deduplication is primarily within an account namespace (not global across accounts).

## Storage Layout

Default filesystem layout (local disk):

- `backup_root/`
  - `<provider>/`
    - `<account_id>/`
      - `current/` (human-browsable backup tree)
      - `blobs/sha256/aa/bb/<full_digest>` (content blob; immutable)
      - `tmp/` (in-progress writes; resumable)
      - `archive/` (optional: if archive/quarantine stores “current” copies here)

Rules:

- Blob writes are **atomic**: write to `tmp/…`, fsync, then rename into `blobs/…`.
- Blob paths are deterministic based on digest to avoid directory hot-spots.
- Blobs are read-only once written.
- Namespacing by provider/account simplifies account removal and avoids cross-account coupling during GC.

### How `current/` relates to `blobs/`

The `current/` tree is what the user browses. Version history lives in `blobs/` and is indexed in the DB.

- On ingest/update: write the new bytes, compute digest, store blob, then materialize into `current/<path>`.
- Materialization strategy:
  - Default: copy blob → `current/<path>` (portable).
  - Optional optimization: hardlink from `blobs/…` into `current/<path>` when on the same filesystem and permitted.
    - If hardlinks are used, treat blobs as immutable; updates create a new blob + replace the `current/` path atomically.

## Data Model (Django)

This is a sketch; exact fields can evolve.

### `BackupBlob`

- `digest` (PK, e.g., `sha256:<hex>`)
- `account_id` (FK)
- `provider` (enum)
- `size_bytes`
- `created_at`
- `storage_path` (optional if derivable from digest)
- `refcount_cached` (optional optimization; authoritative refs come from `FileVersion`)

### `FileVersion`

Represents “this provider item had this content at this time”.

- `id` (PK)
- `account_id` (FK)
- `provider` (enum)
- `provider_item_id` (string)
- `observed_path` (string; local or provider path at capture time)
- `blob_digest` (FK → `BackupBlob.digest`, scoped by provider/account)
- `etag_or_revision` (string; provider-specific)
- `content_modified_at` (datetime; provider metadata if available)
- `captured_at` (datetime; when we recorded it)
- `captured_by_sync_run_id` (FK; for audit)
- `reason` (enum: `update`, `pre_delete`, `manual_snapshot`, `conflict`, `restore_point`)

Uniqueness recommendation:

- `(account_id, provider, provider_item_id, etag_or_revision)` unique when provider revision semantics are strong.
- Otherwise, dedupe by `(account_id, provider, provider_item_id, blob_digest)` with `captured_at` ordering.

### `RetentionPolicy`

Configurable per account/root scope:

- `account_id` (FK)
- `root_id` (FK to sync root)
- `keep_last_n` (int, nullable)
- `keep_days` (int, nullable)
- `max_storage_bytes` (int, nullable; optional global cap)

## Version Capture Rules

Create a `FileVersion` when:

- We download new content (new file or changed file).
- We are about to remove from the active backup view (archive/quarantine move): create `reason=pre_delete` if no version exists for current content.
- User triggers “snapshot now” (optional).

Avoid creating redundant versions:

- If the computed digest matches the latest known version for that `provider_item_id`, do not create a new version (unless `reason` requires it for auditing).

## Restore / Export Semantics

Restore/export is a copy operation from blob → a user-chosen path (commonly back into the active backup view, but also “export elsewhere”):

- Restoring does not mutate existing versions.
- Restoring creates a new `FileVersion` with `reason=restore_point` (optional but recommended for audit).
- Restoring/exporting is a local-only operation; this product does not push changes upstream.

## Retention and Garbage Collection

Retention is applied at the `FileVersion` level; blobs are collected afterward.

### Retention selection

For each `(account_id, provider, provider_item_id)`:

- Keep all versions with `captured_at >= now - keep_days` (if set).
- Additionally keep newest `keep_last_n` versions (if set).
- Always keep any versions referenced by active quarantine entries (if quarantine stores references).

### GC algorithm (safe, incremental)

1. Compute deletable `FileVersion` IDs by retention policy.
2. Delete those `FileVersion` rows in batches.
3. Identify unreferenced blobs: `BackupBlob` with zero referencing `FileVersion`.
4. Delete blob files on disk, then delete `BackupBlob` rows.

Operational notes:

- Run GC as a periodic background task with rate limiting.
- Use DB transactions around version deletion; blob deletion can be “best effort” with reconciliation.
- On startup, reconcile `blobs/` vs DB (optional maintenance command).

## Integrity and Security

- Digest verification on read and optionally on write (streaming hash while downloading).
- Store OAuth tokens securely (separate concern), and consider encrypting the backup store at rest (OS-level disk encryption or application-level encryption if needed).
- Ensure per-account namespace isolation in metadata; the blob store can be shared across accounts safely due to content addressing.

## Alternatives Considered

1. **Per-file copy directories (`/versions/<path>/<timestamp>`)**
   - Pros: easy to inspect manually.
   - Cons: poor dedup; messy with renames/moves; retention/GC harder.

2. **Full snapshot tar/zip archives**
   - Pros: simple backup artifact.
   - Cons: expensive; slow single-file restore; difficult incremental retention.

3. **Chunked/rsync-style delta storage**
   - Pros: better space efficiency for large frequently-edited files.
   - Cons: significantly more complexity; add later if needed.

## Consequences

- Storage usage becomes predictable via retention, with strong deduplication for identical contents.
- Implementation complexity is moderate but contained: immutable blobs + DB index + periodic GC.
- Future extensibility: add chunk manifests for large files without changing `FileVersion` API (store manifest blob instead of raw blob).

## Open Questions

- Do we want a global storage cap (`max_storage_bytes`) with LRU-like eviction across items, or strictly per-item retention?
- Should quarantine be purely “move file into quarantine folder” or “remove from working copy but keep version only”?
- Should we sign blobs or maintain a Merkle index for stronger tamper detection (likely unnecessary for local-only)?
