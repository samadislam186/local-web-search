# OpenSearch Local — Implementation Plan

## Purpose and status conventions

This is the execution plan for `open_search_engine_build_spec.md`. It turns the
architecture contract into incremental, testable deliverables. It does not alter
the decisions in that specification.

Allowed task states are exactly: `pending`, `in progress`, `review`, `test`, and
`done`. A task moves to `done` only after its acceptance criteria and stated
verification pass. Task statuses below reflect the current implementation work.

## Architecture invariants

- Python owns all services, orchestration, interfaces, and control flow; CPU-only
  execution is fully supported and no GPU is required.
- Crawl/build and search/serving run as separate processes. The crawler owns
  mutable state; serving reads a published, immutable generation.
- SQLite in WAL mode is the initial portable mutable crawl-state store, hidden behind
  repositories. It is never the canonical serving-time routing store.
- Every URL takes one versioned canonicalization path and gets a BLAKE3-derived,
  128-bit `UrlId`; collisions are verified and quarantined, never merged.
- Published artifacts are immutable shards with envelopes, checksums, versions,
  manifests, signatures, and atomic generation switches.
- Normal queries use metadata to select a bounded set of shards. They never scan
  the corpus. Reference defaults are 100 MB target shard size and at most ten
  active large shards, both configurable and subject to measurement.
- Lexical BM25/BM25F retrieval is mandatory for the first production baseline. PageRank is one offline
  ranking feature, not a retrieval algorithm. Semantic retrieval is deferred.
- Every large/offline operation streams bounded batches. Untrusted artifacts use
  safe serialization and are checked against pinned Ed25519 release keys.

## Delivery sequence and gates

| Gate | Outcome required to proceed | Status |
|---|---|---|
| G0 | Reproducible Python foundation and a tiny CPU profile | in progress |
| G1 | URL identity and transactional mutable state are correct | in progress |
| G2 | A resumable discovery-to-candidate pipeline works on a tiny Common Crawl sample | in progress |
| G3 | Safe liveness and crawling produce normalized documents and links | in progress |
| G4 | Immutable shards and atomic manifests can publish and recover a generation | in progress |
| G5 | The routing experiment demonstrates acceptable top-K containment within the configured shard budget | in progress |
| G6 | End-to-end CPU search (planner, BM25, ranking, API) meets its measured profile | in progress |
| G7 | Graph authority, evaluation, and production-hardening checks pass | in progress |

G5 is a hard go/no-go gate. Do not scale ingestion before comparing alternative
shard assignments against an exhaustive oracle and recording routing recall,
containment, bytes opened, shard count, and latency.

## Current implementation snapshot

The following is implemented as a portable, CPU-only vertical path, but is not
yet a production-complete release:

- CLI entry points cover manual seeds, resumable Common Crawl CDX ingestion,
  bounded liveness batches, bounded robots-aware crawl batches, index rebuild,
  and local serving.
- URL identity, SQLite/WAL state, schema-version rejection, lifecycle checks,
  provenance, resumable ingestion, lease recovery, retry/backoff, and liveness
  scheduling are implemented and unit-tested.
- Crawled normalized documents are persisted without raw HTML; rebuilds retain
  prior documents, compute generation-local PageRank, publish immutable
  artifacts, and activate a new local generation.
- Serving has bounded metadata routing, lazy shard loading, BM25F retrieval,
  deterministic linear ranking, PageRank authority, snippets, an HTTP API, and
  an agent adapter.

The principal remaining production gaps are representative-corpus routing
evidence, incremental/streaming index construction at scale, signed publisher
workflow, full recovery drills, deployment profiles wired to runtime commands,
structured logs/metrics export, scale benchmarks, and legal/license review.
No gate is `done` until its whole acceptance criterion is verified under its
target operational profile.

## Phase 0 — foundation (G0)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| ARCH-001 | Create the Python package layout, CLI entry points, test layout, and `src` import policy | — | Modules map to the specified subsystems; package imports and CLI smoke test pass | done |
| ARCH-002 | Create validated layered configuration and tiny/laptop/server profiles | ARCH-001 | Defaults include shard and byte budgets; profile override precedence and invalid-config tests pass | done |
| ARCH-003 | Establish structured logging, metrics interfaces, correlation IDs, and error taxonomy | ARCH-001 | JSON logs and no-op/test metrics work without leaking URLs or secrets | done |
| ARCH-004 | Define common IDs, timestamps, result/error types, version fields, and repository/index protocols | ARCH-001 | Type checks and contract tests show subsystem implementations are replaceable | test |
| ARCH-005 | Establish dependency, safe-artifact, formatting, lint, unit/property/integration/performance test policy | ARCH-001 | Local CI command runs deterministically on CPU; prohibited deserializers are rejected by policy tests | test |
| ARCH-006 | Document data directories, temporary-file ownership, retention, and disk-space checks | ARCH-002 | Writes only use configured roots; interrupted temporary work is recoverable/cleanable | test |

## Phase 1 — URL identity and crawl-side state (G1)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| URL-001 | Implement strict URL parsing and unsupported-scheme rejection | ARCH-004 | Malformed, credentialed, relative, IDN, percent-encoded, and unsupported inputs have deterministic outcomes | done |
| URL-002 | Implement versioned canonicalizer v1 | URL-001 | Handles scheme/host casing, IDN, ports, fragments, dot segments, safe slash normalization, and query policy; idempotence property tests pass | done |
| URL-003 | Implement BLAKE3-to-128-bit `UrlId` encoding | URL-002 | Same canonical URL always yields identical 128-bit binary/text representation; 64-bit IDs are impossible in public types | done |
| URL-004 | Implement canonical-string collision verification and quarantine diagnostics | URL-003 | Forced collision fixture cannot merge distinct URLs and creates an inspectable failure record | done |
| STATE-001 | Initialize SQLite schema, WAL, transaction helpers, migrations, and backup/inspection commands | ARCH-004 | WAL is enabled; migration is idempotent; crash/rollback tests preserve consistency | test |
| STATE-002 | Implement candidate, host, liveness, frontier, robots, fetch-history, and ingestion-progress repositories | STATE-001 | No subsystem SQL outside repositories; repository contract tests cover transactions and concurrent leases | test |
| STATE-003 | Define lifecycle transitions and audit fields | URL-003, STATE-002 | Illegal transitions fail; valid transitions preserve timestamps, provenance, retry state, and reason codes | test |

## Phase 2 — discovery and Common Crawl ingestion (G2)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| CC-001 | Discover/select Common Crawl crawl IDs and index partitions | STATE-002 | CLI lists selected metadata and persists chosen crawl/partition plan | test |
| CC-002 | Build a bounded streaming partition reader with retry and cancellation | CC-001 | No full partition list is materialized; fixture proves bounded memory and partial-read recovery | test |
| CC-003 | Persist provenance and idempotent candidate upserts | CC-002, URL-004 | Source, crawl ID, timestamp, partition, original URL, MIME/status/digest metadata are retained; rerun creates no duplicates | test |
| CC-004 | Implement resumable checkpoints and ingestion statistics | CC-003 | Restart resumes at committed checkpoint; counts/errors/throughput are emitted | test |
| CC-005 | Add operator seed and discovered-link source adapters using the same canonicalization pipeline | URL-002, STATE-003 | All sources yield one candidate representation and source provenance | test |

## Phase 3 — liveness and crawler (G3)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| LIVE-001 | Implement DNS resolution/cache and explicit DNS outcomes | STATE-002 | TTL/error-aware cache, timeouts, and retryable/non-retryable classifications are tested | test |
| LIVE-002 | Implement safe TCP/TLS/HTTP probe | LIVE-001 | Per-stage deadlines, redirect limits, body limits, TLS outcome capture, and HTTP status capture work | test |
| LIVE-003 | Implement liveness classifier and exponential backoff | LIVE-002, STATE-003 | One timeout is not permanent death; states include live, blocked, unusable, temporary, dead, and unknown | test |
| LIVE-004 | Add SSRF/address safety policy shared by probes and fetcher | LIVE-002 | Loopback, private, link-local, metadata, and disallowed resolved addresses are blocked by default, including redirect rechecks | test |
| CRAWL-001 | Implement leased priority frontier and per-host scheduling | STATE-002, LIVE-003 | Host concurrency, delay, retry-after, priority, lease expiry, and restart recovery are exercised | test |
| CRAWL-002 | Implement robots retrieval, parsing, caching, and user-agent policy | CRAWL-001 | Disallowed paths are never fetched; robots failures have conservative documented behavior | test |
| CRAWL-003 | Implement safe streaming HTTP fetcher | CRAWL-002, LIVE-004 | Response/decompression limits, redirects, content type, timeouts, and no private-network access are enforced | test |
| CRAWL-004 | Parse HTML into title, description, headings, canonical hint, language hints, main text, anchors, and outgoing links | CRAWL-003 | Parser fixtures cover malformed HTML, base URLs, relative links, and extraction fallback | test |
| CRAWL-005 | Add deterministic n-gram language detection and SimHash near-duplicate fingerprinting | CRAWL-004 | Static data artifacts only; language and Hamming-distance fixtures pass | test |
| CRAWL-006 | Add sitemap discovery and link-to-candidate handoff | CRAWL-004, CC-005 | New URLs preserve discoverer/provenance and follow lifecycle/robots rules | test |

## Phase 4 — immutable data, shards, and manifests (G4)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| STORE-001 | Define versioned shard envelope and typed payload contracts | ARCH-004 | Envelope carries magic, format, type, ID, generation, length, checksum, creation time, compression, and referenced versions | test |
| STORE-002 | Implement streaming shard writer/reader with size accounting | STORE-001 | Configurable target sizing works; malformed/truncated/checksum-invalid shards fail safely | test |
| STORE-003 | Implement URL/document, graph, ranking, and metadata shard payload writers | STORE-002, CRAWL-005 | Typed records round-trip and preserve stable IDs/version references | test |
| STORE-004 | Define manifest schema and compatibility validator | STORE-001 | Manifest lists all artifacts, checksums, dependencies, versions, source crawls, and creation metadata | test |
| STORE-005 | Implement validate-then-atomically-publish generation switch and rollback | STORE-004 | Killed writer, disk-full simulation, bad checksum/signature, and incompatible generation leave prior generation active | test |
| STORE-006 | Implement incremental artifact diff/download planning | STORE-004 | Only missing/changed artifacts are selected; removals are recorded without mutating active shards | test |
| SEC-001 | Implement checksum and Ed25519 signature verification with pinned project roots | STORE-004 | Valid/invalid/revoked/unknown-key fixtures have correct behavior; third-party trust requires explicit fingerprint configuration | test |
| SEC-002 | Enforce safe serialization policy for distributed artifacts | ARCH-005 | Pickle/joblib-style executable artifact inputs are refused; binary/tabular decoders are bounded and validated | test |

## Phase 5 — lexical indexing and routing experiment (G5)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| INDEX-001 | Define project-owned `SearchIndex` and local-result interfaces | STORE-003 | Implementation can be swapped without changing manifests, planner, cache, or ranker APIs | done |
| INDEX-002 | Integrate Tantivy Python bindings behind the abstraction | INDEX-001 | CPU-only index build/open/query works for versioned shards; dependency capability check is recorded | test |
| INDEX-003 | Define mappings/analyzers for title, body, headings, URL, anchor, host/domain fields | INDEX-002 | Field-specific analysis and BM25/BM25F baseline queries have fixture coverage | test |
| INDEX-004 | Implement bounded per-shard top-K retrieval and global candidate merge | INDEX-003 | Each shard returns configured K only; merge is deterministic and bounded | test |
| SPIKE-001 | Assemble representative sample corpus and query/relevance set | INDEX-004 | Dataset provenance, sampling rules, relevance judgments, and scale are reproducibly recorded | test |
| SPIKE-002 | Build exhaustive all-shard oracle and metric runner | SPIKE-001 | Oracle top-K is treated as ground truth for routing measurements | test |
| SPIKE-003 | Implement hash, topic/locality, and hybrid assignment candidates | SPIKE-002 | Same input corpus can be built under every strategy with balanced-size reports | test |
| SPIKE-004 | Build routing metadata candidates and planner simulation | SPIKE-003 | Coarse term/range summaries plus Bloom filters have zero generated false negatives | in progress |
| SPIKE-005 | Measure containment at 1/2/5/10 shards, routing Recall@K, bytes, latency, and candidate recall | SPIKE-004 | Machine-readable benchmark and decision record compare strategies/profile variants | in progress |
| SPIKE-006 | Go/no-go review and select/revise shard strategy | SPIKE-005 | Selection is evidence-backed; failure triggers architecture revision before large ingestion | pending |

## Phase 6 — serving planner, cache, ranker, and API (G6)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| META-001 | Specify and write versioned flat routing metadata files | SPIKE-006 | Metadata is checksumable, mmap-readable, immutable, and independent of SQLite | in progress |
| META-002 | Implement sorted/range presence lookup and compact Bloom lookup | META-001 | Bloom can only admit false positives; round-trip and corruption tests pass | in progress |
| PLAN-001 | Implement deterministic query normalization, token/phrase handling, and MVP query classes | META-002 | Navigational, phrase, URL, domain-filtered, freshness, typo/fuzzy, and broad query fixtures produce explainable plans | in progress |
| PLAN-002 | Score/select candidate shards under max-shard, byte, and latency budgets | PLAN-001 | Plan reports score/reasons; no normal path can exceed configured budget | in progress |
| PLAN-003 | Add early stopping based on confidence, marginal gain, and remaining shard score | PLAN-002, INDEX-004 | Tests prove early exit and safe fallback when budget is exhausted | in progress |
| CACHE-001 | Implement L1 cache for small metadata/query-analysis values | PLAN-001 | Size/TTL/invalidation and hit/miss metrics work | in progress |
| CACHE-002 | Implement bounded L2 large-shard manager with byte accountant | PLAN-002 | Open mappings/readers remain within active-shard and byte limits under concurrency | in progress |
| CACHE-003 | Implement TinyLFU-inspired frequency-plus-recency admission/eviction | CACHE-002 | Scan-resistance benchmark outperforms or justifies alternatives against LRU baseline | in progress |
| RANK-001 | Define candidate feature schema and deterministic linear baseline ranker | INDEX-004, PLAN-003 | BM25, title/phrase/URL/anchor, authority, freshness, quality, duplicate/spam, and feedback fields are versioned | in progress |
| RANK-002 | Implement freshness, authority, duplicate penalty, domain diversity, and explain output | RANK-001 | Diversity runs after recall-safe candidate generation; explain output identifies feature contributions | in progress |
| API-001 | Build process-isolated FastAPI serving application and read-only generation loader | STORE-005, PLAN-003 | Serving does not require mutable crawl SQLite for normal queries | in progress |
| API-002 | Implement health, version, capabilities, stats, and typed POST `/search` endpoints | API-001, RANK-002 | Request validation, filters, pagination, timing/routing summaries, and optional explain mode are covered by API tests | in progress |
| API-003 | Provide an agent-facing `search_web` adapter contract | API-002 | Adapter maps query/top_k/freshness/domain/language without coupling search to an LLM | in progress |

## Phase 7 — graph, quality, evaluation, and operations (G7)

| ID | Work item | Dependencies | Acceptance criteria | Status |
|---|---|---|---|---|
| GRAPH-001 | Build generation-local `UrlId`↔ordinal mapping and compact hyperlink graph writer/reader | STORE-003, CRAWL-004 | Edges use ordinals; mapping validates references and streams bounded batches | test |
| GRAPH-002 | Derive host/domain graphs and retain edge signals needed for later weighting | GRAPH-001 | Logical graph API exposes layers without coupling physical formats | test |
| GRAPH-003 | Implement offline streaming PageRank with damping and dangling-node handling | GRAPH-001 | Small known graphs validate numerically; large runs report bounded memory/progress | test |
| GRAPH-004 | Publish PageRank/ranking shards and integrate global authority feature | GRAPH-003, RANK-001 | Ranker consumes manifest-compatible authority values and handles absent/stale values safely | in progress |
| EVAL-001 | Implement retrieval, routing, cache, PageRank, and performance benchmark harnesses | SPIKE-005, API-002, GRAPH-004 | Reports include Recall/Precision/MRR/NDCG, containment, p50/p95/p99, CPU, RSS, mmap/disk reads, and cache rates | in progress |
| EVAL-002 | Run scale checkpoints at 10^4, 10^5, 10^6, and 10^7 documents | EVAL-001 | Results record hardware, configuration, artifacts, limits, regressions, and next decisions | pending |
| OBS-001 | Instrument query, crawl, planner, retrieval, ranking, cache, graph, and generation metrics | API-002, EVAL-001 | Required metrics are exported and structured logs permit request-to-plan diagnosis | pending |
| OPS-001 | Add recovery drills for corruption, missing data, stale/bad manifest, interrupted publication, DNS outage, HTTP failure, and disk full | STORE-005, API-001 | Every drill has a safe documented outcome; invalid data is never published | pending |
| LEGAL-001 | Produce dependency/data/model license inventory and review gate | ARCH-005 | License compatibility is explicitly recorded before external distribution | pending |

## Deferred after the first production baseline

| ID | Work item | Preconditions | Status |
|---|---|---|---|
| V2-SEM-001 | Optional CPU-friendly semantic/sparse retrieval or reranking | Lexical/routing benchmarks establish a measured need; it never scans the corpus | pending |
| V2-LTR-001 | Learned ranker (likely LightGBM/LambdaMART) | Trustworthy labels/feedback and baseline comparison exist | pending |
| V2-FB-001 | Local feedback event schema, opt-in privacy-preserving aggregation, and local ranking deltas | API client supplies explicit signals; raw private history remains local by default | test |
| V2-COMM-001 | Signed Git contribution format, validation, robust aggregation, review, rollback, and global releases | Threat model and poisoning controls validated | in progress |
| V2-DISC-001 | Certificate Transparency, public domain datasets, feeds, and community discovery adapters | Candidate lifecycle and source limits remain intact | pending |

## Required verification matrix

| Area | Required checks |
|---|---|
| Identity | Canonicalization idempotence, deterministic IDs, collision quarantine, malformed input handling |
| Mutable state | WAL/migration, transactional lifecycle, concurrent leases, resumability/idempotency |
| Network safety | robots, host politeness, redirects, decompression/response limits, SSRF and DNS rebinding defenses |
| Artifacts | Envelope and manifest round trips, checksums, signatures, compatibility, corruption, atomic switch/rollback |
| Retrieval/routing | Oracle-based routing Recall@K and containment, no Bloom false negatives, bounded local/global top-K |
| Resource bounds | Active shard count/bytes, RSS, streaming job behavior, CPU-only profile, no full-corpus normal search |
| Relevance | Recall@K, Precision@K, MRR, NDCG, diversity and ranking explain fixtures |
| Operations | Crash, disk-full, partial download, failed partition, DNS/HTTP outage, missing/corrupt shard recovery |

## Production baseline completion checklist

The first production baseline is `done` only when the following vertical slice is demonstrably reproducible
on a CPU-only tiny profile: a small Common Crawl sample is ingested as canonical
candidate URLs; selected URLs pass liveness and safe crawl; HTML yields normalized
documents/links; documents produce immutable lexical/index/routing artifacts;
the planner routes within configured bounds; BM25 candidates merge with PageRank
and other deterministic features; the REST API returns explainable results; restart,
corruption detection, bounded-memory checks, and benchmarks pass. Semantic search,
distributed operation, hosted community aggregation, GPU acceleration, and
web-scale coverage are explicitly not first-baseline blockers.
