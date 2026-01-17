# TODO

- Define provider auth + scope model (OAuth flows, token storage/encryption, selectable roots).
- Specify sync cursor lifecycle (token invalidation, full-rescan rules, checkpointing/resume).
- Design local path mapping (ID→path mapping, rename/move, normalization, collisions, platform limits).
- Finalize deletion state machine (MissingUpstream thresholds, archive move rules, current vs archive visibility).
- Define retention + GC policy (per-account defaults, batch sizing, scheduling, low-disk behavior).
- Decide storage implementation details (copy vs hardlink materialization, atomic writes, fsync policy, integrity verification).
- Define operational model (Celery/beat setup, logging/audit, reconcile/reindex commands).
- Sketch template-based UX pages (account connect, root selection, status/activity, deleted/missing, restore/export, settings).

