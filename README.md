# Local Web Search

CPU-only Python crawling, immutable lexical indexes, PageRank authority, and a
local FastAPI search service. Python 3.11+ is required. The native backend is
Tantivy; a reference BM25F implementation is available for small experiments.

## Install and test

```powershell
python -m pip install -e ".[dev,tantivy,performance]"
python scripts/ci.py
```

`performance` installs psutil for cross-platform RSS, process read-byte counters,
and resident native-index mapping observations. Missing measurements are null
when the platform or installed dependencies do not provide them.

## Small reproducible run

Use a new output directory. This synthetic demo creates normalized crawl
records, computes PageRank, publishes an index, and queries the HTTP application:

```powershell
python scripts/demo_index.py .demo-run-example
```

The demo defaults to Tantivy; use `--backend reference` for a dependency-light
reference run. To build a native index from your crawl database, use the CLI:

```powershell
open-search init-state crawl.sqlite3
open-search seed crawl.sqlite3 https://example.com/
open-search run-liveness crawl.sqlite3 --max-candidates 100
open-search run-crawl crawl.sqlite3 --max-jobs 100
open-search build-index crawl.sqlite3 artifacts g000001 --backend tantivy --max-documents 10000 --shard-count 10 --profile laptop
open-search serve artifacts --allow-unsigned-local --profile laptop
```

Serving defaults to `127.0.0.1:8080`. Crawl/build commands and serving are separate
processes; normal search reads artifact generations and does not open crawl
SQLite. `--max-documents` bounds the pending input batch; the current crawl-state
builder streams pending records and stages the previous snapshot in temporary
SQLite under the artifact root. Per-shard catalogs identify affected shards;
only those shards are decoded and rebuilt. Publication
chunks default to at most 1,000 documents / 16 MiB source JSON. PageRank nodes,
edges, and iteration vectors stay in disk-backed tables; authority scores are
published in 1,000-row batches and routing summaries stream to disk. Only the
small artifact inventory remains corpus-shard-count dependent in memory.
Unchanged lexical shards and catalogs are hard-linked into the new generation
when supported, with a bounded-copy fallback. Manifest format 2 records their
original generation explicitly; signatures/checksums still validate all artifacts.
Older format-1 generations are readable and migrate to catalogs with one full
bounded rebuild. `--full-rebuild` forces compaction/repartitioning; changing native
binding versions also rebuilds incompatible shards. CLI output reports reused
and rebuilt shard counts. Checksum verification and global PageRank still read
corpus-wide data; copy fallback can rewrite retained bytes on unsupported filesystems.
Representative-corpus RSS and disk-time acceptance still needs real crawl data.

Measure actual process RSS, sampled scratch disk, IO, and update/full-rebuild time:

```powershell
python scripts/rebuild_benchmark.py .scale-verification-rebuild-example --sizes 10000 100000
python scripts/rebuild_benchmark.py .scale-verification-real-example --database D:\data\crawl.sqlite3
```

The second command backs up the source database read-only and modifies only the
benchmark copy. Reports distinguish supplied data from synthetic data and record
sampling/caching limitations. See [rebuild verification](docs/rebuild-verification.md).

`--profile` reads `config/default.toml` plus tiny/laptop/server overrides. Use
`--config-root` when those files are elsewhere. Profiles set target shard size
for builds and active-shard/byte limits for serving. Explicit build sizing wins
over profile defaults. Use a new generation name for each build.

## Signed publication

For a signed native build, provide an existing unencrypted Ed25519 private PEM
file and its key ID. The private key is used only by the build process:

```powershell
open-search build-index crawl.sqlite3 artifacts g000002 --backend tantivy --signing-key release-private.pem --signing-key-id release-1
open-search serve artifacts --pinned-key "release-1=BASE64_RAW_ED25519_PUBLIC_KEY"
```

Activation verifies the detached signature and artifact checksums. The public
key passed to serving must be the trusted 32-byte Ed25519 key encoded as base64.
`--allow-unsigned-local` is an explicit development option for locally built
artifacts. Native packages record the exact Tantivy binding version; a mismatch
requires a compatible installation or a rebuild.

## Query and diagnostics

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/search -Method Post -ContentType application/json -Body '{"query":"python","top_k":10,"freshness":"week","explain":true}'
Invoke-RestMethod http://127.0.0.1:8080/metrics
open-search benchmark-search artifacts python --allow-unsigned-local --runs 20 --warmup-runs 1
```

Freshness means crawl observation time, with day/week/month/year windows.
Unknown, invalid, and future timestamps are excluded when a window is requested.
Filtering operates over bounded retrieved candidates, so a filtered query can
return fewer than the requested number of results.

`/metrics` returns generation-scoped query counters, planner/retrieval/ranking
latency summaries, cache hit/miss/rejection counters, and shard load/read
statistics. Observations retain count/sum/min/max rather than individual samples.
Responses include an `X-Request-ID`; CLI JSON events carry correlation IDs and
omit raw queries, URLs, and secrets. Crawl/liveness and PageRank also emit
structured completion events and expose injectable metric registries.

## Scale verification

```powershell
python scripts/scale_benchmark.py .scale-verification-example --sizes 10000 100000 1000000 10000000 --runs 20
```

The runner requires a new directory, builds in 10,000-document batches, checks
available disk space, and stores `benchmark.json` beside each generation.
Reports include hardware/runtime context, build wall/CPU time, artifact bytes,
search percentiles, RSS, process read-byte deltas, mapped native-index resident
bytes, cache rates, and synthetic topic membership/containment checks.
The scale runner's initial reports include the first cold query; the standalone
benchmark command defaults to one excluded warm-up run. Twenty samples are a
smoke checkpoint, not a statistically strong production tail-latency estimate.

These checkpoints establish synthetic indexing and serving behavior, not web
relevance, broad-query routing quality, peak build RSS, or crawl throughput.
Representative-corpus routing acceptance remains necessary before scaling real
ingestion. See [implementation.md](implementation.md) for acceptance status and
[recovery drills](docs/recovery-drills.md) for failure handling.

## Reliability checks

Continuous crawling and reproducible local crash/restart/serving stress are
documented in [reliability verification](docs/reliability-verification.md).
These tests are synthetic and do not replace deployment-specific soak testing.
