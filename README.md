# Local Web Search

CPU-only Python crawling, immutable lexical indexes, PageRank authority, and a
local FastAPI search service. Python 3.11+ is required. The native backend is
Tantivy; a reference BM25F implementation is available for small experiments.

## Install and test

Install locally on the machine that will crawl and build the index. Docker below
is **search hosting only**; it never crawls or builds an index.

```powershell
python -m pip install -e ".[dev,tantivy,performance]"
python scripts/ci.py
```

Using a virtual environment is recommended: run `python -m venv .venv`, then
`.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on Linux/macOS
before installing. For matching native indexes and the default Docker image,
install `python -m pip install "tantivy==0.26.2"`. The reader requires the exact
Tantivy binding version recorded by the builder.

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
reference run. Its ready-to-serve artifact root is `.demo-run-example/artifacts`.

## Crawl and build an index locally

Run these commands outside Docker. Seeds are URLs, not searchable documents:
liveness and crawling must first fetch and persist their content.

```powershell
open-search init-state crawl.sqlite3
open-search seed crawl.sqlite3 https://example.com/
open-search run-liveness crawl.sqlite3 --max-candidates 100
open-search run-crawl crawl.sqlite3 --max-jobs 100
open-search build-index crawl.sqlite3 artifacts g000001 --backend tantivy --max-documents 10000 --shard-count 10 --profile laptop
open-search serve artifacts --allow-unsigned-local --profile laptop
```

`run-liveness` and `run-crawl` process bounded batches. Repeat them as more URLs
are discovered; use `run-crawl ... --continuous` for a sustained crawl. Respect
robots rules and host delays. If no eligible documents were fetched, there is
nothing to index. `inspect-state crawl.sqlite3` shows lifecycle and frontier counts.
For another pending batch or update, build a new generation, for example
`open-search build-index crawl.sqlite3 artifacts g000002 --backend tantivy --max-documents 10000 --shard-count 10 --profile laptop`.
Publication updates `artifacts/active.json`; the complete artifact root must remain
available, including `generations/`. Keep old generations until all serving
processes using them have stopped, and retain a known-good generation for rollback.

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
open-search build-index crawl.sqlite3 artifacts g000003 --backend tantivy --signing-key release-private.pem --signing-key-id release-1
open-search serve artifacts --pinned-key "release-1=BASE64_RAW_ED25519_PUBLIC_KEY"
```

Activation verifies the detached signature and artifact checksums. The public
key passed to serving must be the trusted 32-byte Ed25519 key encoded as base64.
`--allow-unsigned-local` is an explicit development option for locally built
artifacts. Native packages record the exact Tantivy binding version; a mismatch
requires a compatible installation or a rebuild.

## Docker: host an existing index only

Docker with Linux containers is required. Build the serving image from this
repository (this builds/installs the Python package, **not** the search index):

```powershell
docker build -t local-web-search:serve .
```

The image runs as UID/GID `10001`, binds to `0.0.0.0:8080` inside the container,
and fixes the entrypoint to `open-search serve /indexes`. It defaults to the
`laptop` profile; use `--profile server` only when the host has suitable capacity.
No corpus, crawl database,
or signing key is baked into it. The build context allowlist excludes those files.
Grant the container user read/traverse access to the host artifact directory; do
not make the index world-writable. The default native binding is `0.26.2`; for an
index built with another supported version, use
`docker build --build-arg TANTIVY_VERSION=YOUR_EXACT_VERSION -t local-web-search:serve .`.
Architecture-specific wheel availability and cross-platform native-index
compatibility must be tested for your target host.

Mount the **whole artifact root**, not an individual shard or generation. For
locally built, trusted unsigned artifacts, use PowerShell:

```powershell
$indexPath = (Resolve-Path ./artifacts).Path
docker run -d --name local-web-search --restart unless-stopped --stop-timeout 120 --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=1g -p 127.0.0.1:8080:8080 --mount "type=bind,source=$indexPath,target=/indexes,readonly" local-web-search:serve --profile laptop --allow-unsigned-local
```

Or Linux/macOS:

```bash
docker run -d --name local-web-search --restart unless-stopped --stop-timeout 120 --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=1g -p 127.0.0.1:8080:8080 --mount "type=bind,source=$(pwd)/artifacts,target=/indexes,readonly" local-web-search:serve --profile laptop --allow-unsigned-local
```

For the demo, replace the host artifact path with `.demo-run-example/artifacts`.
For signed releases, replace `--allow-unsigned-local` with
`--pinned-key "release-1=BASE64_RAW_ED25519_PUBLIC_KEY"`. Repeat `--pinned-key` for
multiple trusted keys. Verification is required by default: startup fails if no
pinned key is supplied unless unsigned-local mode is explicitly selected. Only
public keys belong in serving options; never mount the builder's private key.

`/tmp` is needed for native reader extraction even though indexes are read-only.
Size its tmpfs for your workload; tmpfs consumes container/host memory. The
`laptop` and `server` profiles bound cached payloads, not total native RSS or peak
temporary reader usage. Set deployment memory/disk limits after measuring them.
For persistent opt-in feedback, mount a separate writable directory and use
`--enable-local-feedback --feedback-db /feedback/feedback.sqlite3 --feedback-salt-hex YOUR_SECRET_HEX_SALT`;
the index volume should stay read-only. Protect the salt and feedback data.

```powershell
docker logs local-web-search
docker inspect --format '{{.State.Health.Status}}' local-web-search
Invoke-RestMethod http://127.0.0.1:8080/health
```

Startup validates the active generation; health reports process/API liveness,
not complete corpus quality. Large generations can take longer than the default
60-second health-check startup period; override `--health-start-period` as needed.
After building/activating a new generation **on the host**, run
`docker restart local-web-search` to load it. Serving pins its startup generation;
it does not rebuild or automatically hot-swap indexes. Stop it with
`docker stop local-web-search`. Keep the published port bound to loopback as shown
unless an authenticated, TLS-enabled proxy protects access; the API itself does
not implement authentication. Docker restart policies restart exited containers,
not merely unhealthy ones.

## Query and diagnostics

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8080/search -Method Post -ContentType application/json -Body '{"query":"python","top_k":10,"freshness":"week","explain":true}'
Invoke-RestMethod http://127.0.0.1:8080/metrics
open-search benchmark-search artifacts python --allow-unsigned-local --runs 20 --warmup-runs 1
```

For Linux/macOS, query either the local server or Docker-hosted server with:

```bash
curl --fail http://127.0.0.1:8080/search -H 'Content-Type: application/json' -d '{"query":"python","top_k":10,"explain":true}'
curl --fail http://127.0.0.1:8080/version
```

Search only retrieves documents already in the published index; queries do not
crawl the live web. Omit `freshness` to search all observation ages. `/version`
reports the loaded generation, and `/stats` shows shard-cache usage.

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
