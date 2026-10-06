# Data directories and temporary work

All deployed data belongs below the configured `storage.data_root`. The default
configuration resolves that to `config/data`; a server deployment should point it
at a dedicated persistent volume.

`mutable/` contains SQLite state and other replaceable local state. `artifacts/`
contains immutable, published search generations. `temporary/` is exclusively
owned scratch space: interrupted work there is not published and may be removed
by `DataDirectories.clean_interrupted_temporary_work` after the configured
retention interval.

Before a large write, call `DataDirectories.ensure_capacity(required_bytes)`.
It preserves `storage.minimum_free_bytes` for the operating system and fails
before publication if the reserve would be crossed. Paths supplied to this
boundary must be relative and are resolved below the configured data root;
traversal and absolute paths are rejected.

Atomic artifact writers use same-directory temporary siblings so replacement is
atomic on one filesystem. They clean those files on ordinary failures. For a
hard process interruption, temporary-root recovery is safe because it never
touches `artifacts/` or `mutable/`.
