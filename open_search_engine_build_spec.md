# OpenSearch Local — Complete Build Specification

> **Purpose:** This document is the implementation specification for building a fresh, open-source, Python-only, CPU-first web search engine from scratch.
>
> It is written for a coding agent or engineering team. It is intentionally detailed, prescriptive where decisions are already made, and explicit about which areas require measurement rather than guesswork.
>
> **Primary goal:** build a local-first search engine that can ingest a very large URL universe, discover and validate live pages, crawl and index the web incrementally, construct graph-derived authority signals, retrieve and rank results quickly on CPU, expose a search API to agents/apps, and improve over time through local and community-contributed metadata/ranking updates.
>
> **Non-goal:** recreate Google infrastructure literally.

---

# 0. Executive Summary

The system must:

1. Be implemented **entirely in Python** as the project language.
2. Run correctly on **CPU-only machines**; GPU must never be required.
3. Use Common Crawl and other public sources as **URL discovery seeds**, not as the search engine itself.
4. Maintain its own canonical URL identity, liveness state, crawl state, graph, index, metadata, ranking signals, and API.
5. Keep large persisted search data in **small independently versioned shards**.
6. Treat **100 MB per shard** and **10 simultaneously active large shards** as the initial reference profile, not immutable constants.
7. Enforce a bounded runtime working set.
8. Never scan the entire corpus for a normal query.
9. Use a compact **metadata routing layer** to determine which shards should be opened and in what order.
10. Keep frequently accessed data in a separate in-memory cache.
11. Use **lexical retrieval/BM25 as the baseline retrieval mechanism**.
12. Treat PageRank as **one ranking feature**, not the search algorithm.
13. Build graph authority using Markov-chain/PageRank-style computation offline.
14. Add optional CPU-friendly semantic/sparse retrieval later, but never make it mandatory.
15. Use community feedback to improve routing metadata and ranking, but only through validated, versioned contributions.
16. Use **SQLite + WAL** for mutable crawl-side state in the MVP.
17. Use **128-bit BLAKE3-derived UrlId** values.
18. Use **Ed25519 signatures with pinned project trust roots** for signed releases/artifacts.
19. Prohibit model/data deserialization formats that can execute arbitrary code.
20. Keep crawler/discovery workers in a **separate process from the serving/query process**.
21. Make updates incremental and atomic.
22. Keep the system inspectable, debuggable, reproducible, and open-source friendly.

---

# 1. Product Vision

## 1.1 Why this project exists

Agent frameworks can run locally, and local LLMs can run without API fees, but most practical agents still depend on commercial web search APIs.

This project removes that dependency.

The search engine should eventually provide a tool interface such as:

```text
search_web(query="latest CUDA memory optimization techniques", top_k=10)
```

and return high-quality ranked web results without depending on a paid search provider.

The system is intended to become:

```text
Internet
   ↓
Discovery
   ↓
Liveness
   ↓
Crawler
   ↓
Index + Web Graph + Metadata
   ↓
CPU Search Engine
   ↓
Search API
   ↓
Local/Remote Agent
```

---

# 2. Hard Architectural Principles

## 2.1 Python is permanent

The entire project is Python.

Do not design for a future Rust/Go/C++ rewrite.

Python may call optimized native libraries where useful, including:

- SQLite
- NumPy
- SciPy
- Tantivy Python bindings if selected
- PyArrow
- BLAKE3 bindings
- compression libraries
- mmap-backed readers
- optimized ML runtimes

But all project-owned services, interfaces, orchestration, control flow, configuration, and APIs remain Python.

If a component is slow:

1. measure it,
2. improve the algorithm,
3. reduce the amount of work,
4. improve data layout,
5. use batching,
6. use mmap,
7. use native Python-accessible libraries,
8. improve caching,
9. precompute more work.

Do not solve performance problems by changing the project language.

---

## 2.2 CPU-first

The engine must work on a normal CPU-only machine.

GPU is never required for:

- crawling
- URL discovery
- DNS
- liveness checks
- indexing
- BM25
- graph processing
- PageRank
- metadata routing
- caching
- ranking
- API serving

Optional future semantic models may use a GPU if the operator explicitly enables it, but the baseline product must remain fully functional without one.

---

## 2.3 Bounded working set

The corpus may become hundreds of gigabytes or more.

The query runtime must not require the whole corpus in memory.

Initial reference profile:

```text
max_large_shard_size_mb = 100
max_active_large_shards = 10
max_active_large_shard_bytes ≈ 1 GB
```

These are configuration defaults.

The real invariant is:

> The runtime must enforce a bounded large-data working set.

A future profile may use:

```text
50 MB × 10
128 MB × 8
100 MB × 20
```

if measurements justify it.

---

## 2.4 Never full-scan the corpus for normal search

Forbidden query design:

```text
query
 ↓
load shard 1..10
 ↓
search
 ↓
load next 10
 ↓
search
 ↓
repeat until corpus exhausted
```

Required design:

```text
query
 ↓
query analysis
 ↓
metadata router
 ↓
candidate shard ranking
 ↓
open only highest-value shards
 ↓
retrieve candidates
 ↓
early stop when confidence/budget reached
 ↓
rank top candidates
```

Full-corpus processing is only allowed for offline jobs such as:

- rebuilding graph features
- recomputing PageRank
- generating routing metadata
- compaction
- global statistics
- offline evaluation

Even offline jobs must stream bounded batches.

---

# 3. Repository Layout

Recommended initial repository:

```text
project/
├── README.md
├── pyproject.toml
├── LICENSE
├── config/
│   ├── default.yaml
│   ├── dev.yaml
│   └── profiles/
│       ├── tiny.yaml
│       ├── laptop.yaml
│       └── server.yaml
│
├── src/
│   └── search_engine/
│       ├── __init__.py
│       │
│       ├── cli/
│       ├── config/
│       ├── discovery/
│       ├── common_crawl/
│       ├── dns/
│       ├── liveness/
│       ├── crawler/
│       ├── canonicalization/
│       ├── identity/
│       ├── parser/
│       ├── dedup/
│       ├── storage/
│       ├── shards/
│       ├── manifest/
│       ├── metadata/
│       ├── routing/
│       ├── index/
│       ├── graph/
│       ├── pagerank/
│       ├── retrieval/
│       ├── ranking/
│       ├── semantic/
│       ├── feedback/
│       ├── community/
│       ├── cache/
│       ├── api/
│       ├── security/
│       ├── observability/
│       └── evaluation/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── property/
│   ├── failure/
│   └── performance/
│
├── benchmarks/
├── docs/
├── tools/
├── examples/
└── data/
    ├── mutable/
    ├── manifests/
    ├── url/
    ├── index/
    ├── graph/
    ├── ranking/
    ├── metadata/
    ├── feedback/
    ├── cache/
    └── tmp/
```

---

# 4. Process Model

There must be at least two logical processes.

## 4.1 Search/Serving Process

Responsibilities:

- load active manifest
- load routing metadata
- query analysis
- shard selection
- shard cache
- retrieval
- ranking
- search API
- diagnostics
- metrics

This process should treat the published corpus as read-mostly/immutable.

## 4.2 Crawl/Build Process

Responsibilities:

- discovery
- Common Crawl ingestion
- DNS
- liveness
- frontier
- crawling
- parsing
- deduplication
- index segment generation
- graph segment generation
- metadata generation
- new manifest generation
- compaction
- offline PageRank
- corpus publication

This process is isolated from query serving.

---

# 5. Mutable Crawl-Side Store

## Decision

Use **SQLite + WAL for MVP**, behind repository interfaces.

This is the canonical MVP mutable store for:

- candidate URL state
- host state
- liveness state
- crawl frontier
- crawl leases
- retry state
- robots state
- fetch history
- pending publication state
- Common Crawl ingestion progress

The serving process must not rely on this database for ordinary search.

## 5.1 Why SQLite

Advantages:

- Python standard ecosystem
- reliable transactions
- inspectable with SQL
- WAL supports concurrent readers/writers
- easy debugging
- zero external service
- excellent for initial local deployment
- easy backup/recovery
- simple operational model

## 5.2 Repository abstraction

Do not scatter SQL through subsystem code.

Use repositories conceptually like:

```text
CandidateRepository
FrontierRepository
LivenessRepository
HostRepository
RobotsRepository
IngestionProgressRepository
```

Storage implementation can later change without rewriting business logic.

## 5.3 Revisit conditions

Reconsider the backend only if measured at meaningful scale.

Suggested triggers:

- p95 lease/commit latency > 100 ms
- SQLite CPU > 20% of crawl-worker CPU
- severe WAL checkpointing stalls
- operational contention at ≥10^7 records
- benchmarked throughput insufficient for crawl target

---

# 6. URL Discovery

The system cannot ask DNS for "all sites."

It needs candidate URL sources.

## 6.1 Initial sources

MVP sources:

1. Common Crawl indexes
2. links extracted from crawled pages
3. redirects discovered while crawling
4. sitemap URLs
5. operator seed URLs

Later sources:

- Certificate Transparency logs
- public domain datasets
- user/community contributions
- feeds
- specialized domain lists

## 6.2 Discovery is not liveness

A discovered URL means:

```text
candidate
```

not:

```text
currently live
```

Lifecycle:

```text
DISCOVERED
   ↓
CANONICALIZED
   ↓
IDENTIFIED
   ↓
LIVENESS_PENDING
   ↓
LIVE / BLOCKED / DEAD / UNKNOWN
   ↓
CRAWLABLE
   ↓
CRAWLED
   ↓
INDEXED
```

---

# 7. Common Crawl Integration

Common Crawl is the initial large-scale URL seed source.

Do not treat it as the final search index.

Pipeline:

```text
Common Crawl crawl/index
        ↓
partition discovery
        ↓
bounded streaming reader
        ↓
URL + metadata
        ↓
canonicalization
        ↓
UrlId
        ↓
deduplication
        ↓
candidate store
        ↓
later DNS/liveness/crawl
```

## 7.1 Requirements

The implementation must:

- allow crawl ID selection
- discover index partitions
- process partitions incrementally
- persist ingestion progress
- be resumable
- be idempotent
- keep memory bounded
- never build a multi-billion-element Python list
- preserve provenance

## 7.2 Provenance fields

Where available, preserve:

```text
source = common_crawl
crawl_id
crawl_timestamp
partition
original_url
mime
status
digest
record metadata
```

These fields do not replace project URL identity.

## 7.3 Common Crawl URL ≠ live URL

Imported records should start with a state like:

```text
SEEN_IN_CRAWL
```

Liveness must be checked independently.

---

# 8. URL Canonicalization

Implement exactly one canonicalization system.

Inputs from Common Crawl, crawling, sitemaps, and manual seeds must all use the same canonicalizer.

Handle:

- lowercase scheme and hostname
- IDN/punycode normalization
- default ports
- fragments
- repeated slashes where safe
- dot segments
- percent encoding
- trailing slash policy
- empty path
- query parameters
- tracking parameter policy
- malformed encodings
- credentials
- unsupported schemes

Never aggressively remove arbitrary query parameters without evidence that they are non-semantic.

Canonicalization must be versioned:

```text
canonicalization_version = 1
```

Changing canonicalization can change identity and may require migrations.

---

# 9. URL Identity

## Decision

Use **BLAKE3-derived 128-bit UrlId**.

Concept:

```text
canonical_url
    ↓
BLAKE3
    ↓
first 128 bits
    ↓
UrlId
```

Store canonical string verification where identity is resolved.

A collision must never silently merge two different canonical URLs.

On collision:

```text
fail-stop / quarantine / diagnostics
```

Do not use 64-bit IDs.

Graph edges should use generation-local dense ordinals when possible so 128-bit IDs do not inflate every edge.

---

# 10. Liveness Pipeline

DNS alone means only that a name may resolve.

Define explicit liveness states.

Suggested:

```text
SEEN_IN_CRAWL
DNS_RESOLVABLE
LIVE
LIVE_BUT_BLOCKED
LIVE_BUT_UNUSABLE
TEMPORARILY_UNAVAILABLE
DEAD
UNKNOWN
```

Pipeline:

```text
candidate hostname
    ↓
DNS
    ↓
TCP
    ↓
TLS (for HTTPS)
    ↓
HTTP
    ↓
classify
```

Track separately:

```text
dns_last_checked
dns_status
http_last_checked
http_status
redirect_target
tls_status
failure_reason
retry_after
```

Use backoff and retry policies.

Do not classify one timeout as permanent death.

---

# 11. Crawler

## 11.1 Frontier

The frontier should schedule URLs, not simply hold a FIFO list.

Priority inputs may include:

- first-seen source
- host politeness
- estimated value
- freshness need
- previous change frequency
- retry state
- domain quality
- PageRank/authority later
- sitemap priority only as weak input

Use per-host limits.

## 11.2 Politeness

Mandatory:

- robots.txt awareness
- user-agent configuration
- per-host request rate
- concurrency cap per host
- exponential backoff
- Retry-After support
- timeout limits
- response size limits

## 11.3 Fetch safety

Prevent:

- infinite redirects
- huge bodies
- decompression bombs
- local/internal SSRF targets
- invalid IP ranges if configured
- loopback
- metadata services
- private-network access by default

## 11.4 Parsing

For HTML:

- title
- meta description
- canonical tag
- language hints
- headings
- main text
- anchor text
- outgoing links
- structured metadata where useful
- HTTP headers
- content hash

Do not store full raw HTML forever unless configured.

---

# 12. Language Detection

MVP language detection must be a **data-driven CPU component**, not a code-executing model artifact.

Preferred MVP approach:

- n-gram language profiles
- compact static tables
- deterministic CPU inference

This keeps model artifact handling out of the MVP.

---

# 13. Near-Duplicate Detection

## Decision

Use **SimHash for MVP**.

Pipeline:

```text
normalized main text
    ↓
tokens/features
    ↓
SimHash
    ↓
fingerprint
```

Use Hamming-distance based near-duplicate grouping.

Later evaluate:

- MinHash
- LSH
- shingles
- more advanced document fingerprints

Do not add complexity until SimHash quality is measured.

---

# 14. Data Sharding

## 14.1 Shard constraints

Reference defaults:

```text
target_shard_size_mb = 100
max_active_shards = 10
max_active_shard_bytes = 1 GiB
```

The engine must support configuration.

## 14.2 Immutable published shards

Published search shards should be immutable.

Updates create new shards/segments and a new manifest.

Do not mutate published shard files in place.

## 14.3 Common envelope

Every project-owned shard should have a common outer envelope containing:

```text
magic
format_version
shard_type
shard_id
generation
payload_length
checksum
creation_time
compression
metadata/version references
```

The payload format may differ by shard type.

Examples:

```text
URL shard       → compact typed records / columnar
graph shard     → compressed adjacency
ranking shard   → dense arrays
metadata shard  → sorted compact records
index shard     → search-index-native format
```

---

# 15. Shard Assignment — Critical Experimental Area

This is one of the highest-risk design decisions.

A poor assignment strategy can force most queries to touch more than the allowed shard budget.

Do not lock this based on intuition alone.

## 15.1 Candidate strategies

### Hash partitioning

Pros:

- balanced

Cons:

- destroys query locality
- likely touches many shards

### Topic partitioning

Pros:

- good topical locality

Cons:

- hard boundaries
- imbalance
- multi-topic documents

### Domain/host partitioning

Pros:

- operational locality

Cons:

- broad queries span many hosts

### Lexical range partitioning

Pros:

- predictable term routing

Cons:

- documents contain many terms

### Hybrid locality-aware partitioning

Potentially combine:

```text
language
topic/coarse cluster
term signatures
size balancing
```

## 15.2 Required spike

Build a synthetic/real-sample routing experiment before large-scale ingestion.

Measure:

```text
Routing Recall@K
Top-K containment ≤1 shard
Top-K containment ≤2 shards
Top-K containment ≤5 shards
Top-K containment ≤10 shards
bytes opened per query
shards opened per query
latency
candidate recall
```

The system must favor **routing recall** over routing precision.

Missing the shard with the best result is worse than loading one unnecessary shard.

---

# 16. Metadata Is a First-Class Index

The metadata layer is the brain that decides where to search.

It must remain small enough to keep mostly in memory or mmap efficiently.

Metadata may include:

```text
shard_id
generation
size
document_count
host/domain summary
language distribution
topic distribution
time range
term presence summary
term ranges
Bloom filters
hotness
historical hit rate
historical routing score
freshness distribution
PageRank min/max/quantiles
index capabilities
checksum
```

---

# 17. Presence Structure

## Decision

Use a hybrid:

```text
coarse sorted term/range metadata
+
compact Bloom filters
```

Goals:

- cheap exclusion of impossible shards
- very low false-negative risk
- acceptable false positives

Bloom filters may produce false positives.

They must not produce false negatives if generated correctly.

Do not use Cuckoo filters by default unless benchmarks show a real advantage.

---

# 18. Routing Metadata Storage

Use **versioned flat files** as the canonical routing metadata format.

Do not use SQLite as serving-time canonical routing metadata.

Reasons:

- immutable
- distributable
- mmap-friendly
- checksum-friendly
- atomic version switch
- reproducible
- easy to publish

SQLite remains appropriate for mutable crawl-side state.

---

# 19. Query Planner

This subsystem is critical.

Pipeline:

```text
query
  ↓
normalize
  ↓
analyze tokens/phrases
  ↓
classify query
  ↓
consult routing metadata
  ↓
score candidate shards
  ↓
check cache
  ↓
open best shard(s)
  ↓
retrieve local top-K
  ↓
decide continue/stop
  ↓
global merge
```

## 19.1 Query types

Possible classifications:

- exact/navigational
- informational
- technical
- broad exploratory
- freshness-sensitive
- domain-filtered
- phrase-heavy
- typo/fuzzy
- URL lookup

MVP need not build a complex classifier; simple deterministic rules are acceptable.

## 19.2 Planner output

Conceptually:

```json
{
  "query_id": "...",
  "candidate_shards": [
    {"id": "...", "score": 0.94, "reason": ["term", "topic"]},
    {"id": "...", "score": 0.88, "reason": ["term"]}
  ],
  "max_shards": 10,
  "byte_budget": 1073741824
}
```

## 19.3 Early stopping

Do not always load 10 shards.

If 2 shards produce strong enough candidates, stop.

Possible stopping signals:

- enough high-scoring candidates
- diminishing marginal candidate gain
- shard score below threshold
- latency budget exhausted
- byte budget exhausted

---

# 20. Retrieval

## 20.1 Baseline

Use an inverted lexical index with BM25/BM25F-style scoring.

Fields:

- title
- main text
- headings
- URL tokens
- anchor text
- domain/host tokens

Do not use one shared weight for every field.

Initial weights are placeholders and must be benchmarked.

## 20.2 Tantivy decision

Preferred MVP choice:

> Use Tantivy through Python bindings behind a project-owned index abstraction.

Do not let Tantivy determine the whole architecture.

Project owns:

- shards
- manifest
- routing
- cache
- update lifecycle
- query planning
- ranking

Search-index library owns:

- term dictionary
- posting lists
- BM25
- segment internals
- efficient lexical retrieval

If the chosen Python binding is not mature enough, implement a minimal alternative only after documenting the gap.

---

# 21. Multi-Stage Ranking

Required normal query path:

```text
Query
 ↓
metadata routing
 ↓
BM25 / lexical retrieval
 ↓
local top candidates
 ↓
global candidate merge
 ↓
cheap feature computation
 ↓
multi-signal ranking
 ↓
top results
```

Optional later path:

```text
top 50/100
 ↓
CPU semantic/sparse reranker
 ↓
top 10/20
```

Never run expensive semantic inference over the entire corpus.

---

# 22. PageRank / Markov Authority

Build PageRank offline.

Basic weighted transition:

```text
P_ij = w_ij / sum_k(w_ik)
```

Stationary authority vector:

```text
π = πP
```

Use damping and dangling-node handling.

Do not assume raw inbound-link count equals importance.

Potential link weights may consider:

- source authority
- repeated-template links
- sitewide footer links
- spam suspicion
- internal vs external links
- nofollow-like signals
- anchor context
- domain diversity

MVP can begin with simpler weights but must store enough data to improve later.

---

# 23. Physical Graph Design

Use **separate physical graph layers with a unified logical API**.

Possible stores:

```text
graph/hyperlink/
graph/domain/
graph/host/
graph/query_page/       # later
graph/interaction/      # later
graph/semantic/         # optional later
```

The hyperlink graph is primary for MVP.

Edges should use compact generation-local ordinals.

Mapping:

```text
UrlId (128-bit)
   ↕
generation-local ordinal (u64)
```

This keeps graph storage compact.

---

# 24. Topic Authority

Do not block MVP on an ML topic model.

Initial topic information can come from:

- title keywords
- headings
- domain patterns
- URL path tokens
- anchor text
- term statistics
- a small maintained taxonomy

Store:

```text
topic_id
confidence
source
topic_algorithm_version
```

Later models can replace the heuristic without changing the metadata contract.

---

# 25. Ranking Features

Candidate ranking should support features such as:

```text
BM25
title_match
phrase_match
URL_match
anchor_match
global_PageRank
domain_authority
topic_authority
freshness
content_quality
duplicate_penalty
spam_penalty
domain_diversity
feedback_score
optional_semantic_score
```

MVP can use a deterministic weighted linear model.

All initial weights are **untuned starting points**.

Never claim they are optimal.

---

# 26. Learning to Rank

Later, replace/manual-complement the linear model with a CPU-friendly learned ranker.

Candidates:

- linear regression/ranker
- logistic model
- LightGBM/LambdaMART
- XGBoost ranking

Preferred likely future path:

```text
hand-tuned linear baseline
        ↓
collect evaluation labels/feedback
        ↓
LightGBM/LambdaMART
```

Do not build LTR until enough trustworthy data exists.

---

# 27. Search Result Diversity

Prevent the top 10 from being ten near-identical pages from one domain.

Post-ranking diversity may consider:

- max pages per domain
- duplicate groups
- similarity penalty
- topic diversity
- source diversity

Keep this late in the pipeline so recall is not harmed.

---

# 28. Caching

Use multiple cache levels.

## 28.1 L1 — tiny hot metadata

Examples:

- query tokenization
- routing lookups
- tiny shard summaries
- popular domains

## 28.2 L2 — active large shard cache

This is the bounded pool of large data shards.

Reference maximum:

```text
10
```

Use a frequency-aware policy.

Preferred architecture:

> TinyLFU-inspired / frequency + recency admission/eviction.

Pure LRU is vulnerable to scans.

## 28.3 Query-result cache

Do not include in MVP.

Defer to V2 due to:

- privacy
- freshness
- invalidation
- personalization
- cache poisoning

---

# 29. Local Top-K + Global Merge

Every loaded shard should return only its strongest candidates.

Do not move huge result sets between layers.

Example:

```text
shard A → top 100
shard B → top 100
shard C → top 100
              ↓
         global merge
              ↓
            top 200
              ↓
           reranking
              ↓
            top 10
```

---

# 30. Feedback

MVP feedback should be designed, but not allowed to dominate ranking.

Signals may include:

- result impression
- click/select
- explicit up/down feedback
- client-reported citation/use
- query reformulation
- repeated query

Be careful:

An HTTP API cannot directly observe dwell time or pogosticking unless clients report it.

Do not pretend these signals are available by default.

---

# 31. Community Learning

Community learning should be optional and initially conservative.

Local instance:

```text
global release
    ↓
local usage
    ↓
local aggregated observations
    ↓
signed delta
```

Community flow:

```text
signed local deltas
    ↓
validation
    ↓
aggregation
    ↓
review
    ↓
new versioned metadata/ranking release
```

## 31.1 MVP transport

Use a **Git-based contribution/release workflow** initially.

Do not require a hosted central service.

A hosted aggregator can be added later.

## 31.2 Never upload raw private history by default

Prefer aggregated contributions.

Examples:

```text
query-class → shard hit statistics
page/query aggregate score deltas
routing usefulness counters
```

---

# 32. Community Poisoning Resistance

Assume all community input is untrusted.

Threats:

- fake clicks
- bot installations
- coordinated Sybil users
- SEO campaigns
- manipulated routing metadata
- malicious shard contributions
- poisoned ranking deltas

Controls:

- contribution signatures
- per-source limits
- confidence thresholds
- diversity requirements
- robust aggregation
- anomaly detection
- time smoothing
- quarantine
- manual review for global releases
- rollback

One contributor must never be able to dramatically change global ranking.

---

# 33. Security of Published Artifacts

## Decision

Use:

```text
Ed25519
+
pinned project release public keys
```

Fresh installations trust keys bundled/pinned with the software.

Third-party datasets require explicitly configured fingerprints.

TOFU may exist only as an explicit operator mode.

FIPS/FedRAMP is not an MVP requirement.

---

# 34. Safe Serialization

Production runtime must never load a format where deserialization can execute arbitrary code.

Forbidden examples include untrusted Python pickle/joblib-style arbitrary object loading for downloaded artifacts.

If joblib is used locally for trusted developer-generated experiments, it must not become the community artifact distribution format.

For future ML models prefer safe formats such as:

- SafeTensors where appropriate
- validated ONNX with strict operator policies
- project-owned binary/tabular formats

Positive model format decision is deferred until models are actually needed.

---

# 35. Manifest System

Every published corpus generation must have a manifest.

The active manifest describes:

```text
generation ID
format versions
canonicalization version
UrlId version
index version
graph version
ranking version
routing metadata version
all shard files
size
checksum
dependencies
creation time
source crawl IDs
```

Example conceptual tree:

```text
manifests/
  generation-000042/
      manifest.json
      routing.meta
      checksums
```

Publication must be atomic.

---

# 36. Atomic Update Process

Required:

```text
download/build new generation
    ↓
validate every artifact
    ↓
verify signatures
    ↓
verify checksums
    ↓
verify compatibility
    ↓
write new manifest
    ↓
atomic active-generation switch
```

If anything fails:

```text
keep previous generation active
```

No half-updated state.

---

# 37. Incremental Updates

Users should not redownload the entire corpus.

A new release should identify:

```text
unchanged shards
new shards
changed shards
removed shards
new metadata
new ranking data
```

Only missing/changed artifacts are fetched.

Immutable shard design makes this straightforward.

---

# 38. Search API

Minimum endpoints:

```text
GET  /health
GET  /version
GET  /capabilities
GET  /stats
POST /search
```

Possible search request:

```json
{
  "query": "best CPU inference engine for LLMs",
  "top_k": 10,
  "freshness": null,
  "domain_filter": null,
  "language": "en",
  "explain": false
}
```

Possible response:

```json
{
  "query": "...",
  "results": [
    {
      "rank": 1,
      "url": "https://...",
      "title": "...",
      "snippet": "...",
      "score": 0.91,
      "domain": "...",
      "crawl_time": "...",
      "freshness": 0.72
    }
  ],
  "routing": {
    "shards_considered": 18,
    "shards_opened": 3
  },
  "timing_ms": {
    "total": 54
  }
}
```

`explain=true` may add internal ranking features for debugging.

---

# 39. Agent Integration

Expose a simple tool contract:

```text
search_web(
    query,
    top_k=10,
    freshness=None,
    domain_filter=None,
    language=None
)
```

The search engine remains independent from the LLM.

LLM/agent responsibilities:

- formulate query
- call search
- read snippets/pages
- synthesize response

Search engine responsibilities:

- discovery
- retrieval
- ranking
- result evidence

---

# 40. Observability

Metrics should include:

```text
query_latency_p50
query_latency_p95
query_latency_p99
planner_latency
retrieval_latency
ranking_latency
shards_considered
shards_opened
active_shard_bytes
L1_cache_hit_rate
L2_cache_hit_rate
routing_recall
crawl_fetch_rate
crawl_success_rate
dns_success_rate
frontier_depth
index_documents
graph_nodes
graph_edges
manifest_generation
```

Structured logging is required.

---

# 41. Evaluation

The engine is not "accurate" until measured.

Create an evaluation subsystem early.

## 41.1 Retrieval metrics

- Recall@K
- Precision@K
- MRR
- NDCG

## 41.2 Routing metrics

Critical:

```text
Routing Recall@K
Top-K containment by shard budget
average shards opened
p95 shards opened
bytes opened
```

## 41.3 Performance metrics

- p50/p95/p99 latency
- CPU
- peak RSS
- mmap bytes
- disk reads
- cache hit rates

## 41.4 Corpus scale checkpoints

At minimum test:

```text
10^4 docs
10^5 docs
10^6 docs
10^7 docs
```

Do not jump immediately to billions.

---

# 42. Placeholder Values

The following values are not facts:

```text
100 MB shard target
10 active shards
p95 < 300 ms
BM25 field weights
BM25 k1/b
planner weights
linear ranker weights
LTR minimum sample size
community contributor minimum
cache hit-rate expectations
```

Treat them as:

```text
initial defaults / hypotheses
```

Every one must eventually be validated.

---

# 43. Performance Profiles

## Tiny

Target:

```text
2–4 GB RAM
CPU only
small subset corpus
```

## Laptop

Target:

```text
8–16 GB RAM
SSD
CPU only
reference 100 MB × 10 working set
```

## Server

Target:

```text
32+ GB RAM
NVMe
higher concurrency
same formats
larger cache
```

All profiles use the same artifact formats.

---

# 44. Offline Jobs

Offline processing includes:

- shard compaction
- PageRank
- graph statistics
- metadata generation
- Bloom construction
- evaluation
- corpus statistics
- community aggregation
- index rebuild

Offline processing must also remain bounded in memory:

```text
stream shard
process
write output
release
next shard
```

---

# 45. Failure Recovery

Must handle:

- process crash
- disk full
- corrupt shard
- bad manifest
- failed Common Crawl partition
- DNS outages
- HTTP failures
- partial download
- failed signature
- checksum mismatch
- interrupted publication

Never publish invalid data.

---

# 46. Legal and Licensing

Project license recommendation:

```text
Apache-2.0
```

but final license/dependency/data compatibility requires legal verification.

Maintain a license inventory for:

- Python packages
- Tantivy bindings
- Common Crawl data
- models
- datasets
- optional tooling

Treat Quickwit/other AGPL projects as architectural references unless dependency licensing is intentionally accepted.

---

# 47. Testing Strategy

## Unit

- URL canonicalization
- BLAKE3 UrlId
- Bloom filter serialization
- shard envelope
- metadata lookup
- planner scoring
- rank scoring
- cache admission/eviction
- liveness classification

## Property tests

- canonicalization idempotence
- identity determinism
- shard checksums
- no false negatives in generated presence structures
- manifest round-trip

## Integration

```text
Common Crawl sample
  ↓
candidate store
  ↓
liveness
  ↓
crawl
  ↓
parse
  ↓
index
  ↓
graph
  ↓
manifest
  ↓
query
```

## Failure

- corrupt shard
- missing shard
- stale manifest
- killed writer
- malformed URL
- DNS timeout
- redirect loop
- large response
- malicious compressed input

---

# 48. Build Order

Do not build everything simultaneously.

## Phase 0 — Foundation

- repository
- configuration
- logging
- metrics
- common interfaces
- tests
- file/version conventions

## Phase 1 — Identity + Mutable State

- canonicalizer
- UrlId
- SQLite/WAL repositories
- URL lifecycle

## Phase 2 — Common Crawl Seed Ingestion

- crawl discovery
- partition streaming
- resumability
- provenance
- candidate ingestion

## Phase 3 — Liveness

- DNS
- HTTP probe
- state classification
- retry/backoff

## Phase 4 — Crawler

- frontier
- robots
- fetch
- parse
- links
- content normalization

## Phase 5 — Storage/Shards

- shard envelope
- manifest
- URL shards
- metadata shards
- atomic publication

## Phase 6 — Lexical Search

- index abstraction
- Tantivy integration
- BM25
- local top-K

## Phase 7 — Routing Spike

- generate sample corpus
- compare shard strategies
- build routing oracle
- validate ≤10 shard hypothesis

Do not proceed to very large-scale corpus generation until this phase produces acceptable results.

## Phase 8 — Query Planner + Cache

- routing metadata
- term ranges
- Bloom filters
- planner
- L1
- L2
- bounded shard manager

## Phase 9 — Graph + PageRank

- hyperlink graph
- ordinal mapping
- PageRank
- authority shards

## Phase 10 — Ranking

- multi-signal ranker
- freshness
- authority
- diversity
- explain mode

## Phase 11 — API

- FastAPI or equivalent Python API
- search endpoint
- diagnostics
- agent tool integration

## Phase 12 — Evaluation/Optimization

- relevance set
- routing recall
- latency
- CPU
- memory
- shard budget tuning

## Phase 13 — Feedback

- local observations
- safe aggregates
- opt-in contribution

## Phase 14 — Community Releases

- signatures
- Git-based contribution
- validation
- metadata/rank release

## Phase 15 — Optional Semantic Layer

Only after lexical + graph ranking has real benchmarks.

---

# 49. Implementation Status

All tasks start `Pending` unless real code and verification already exist.

Allowed statuses only:

```text
Pending
In Progress
Review
done
```

## Foundation

| ID | Task | Status |
|---|---|---|
| ARCH-001 | Create Python project structure | Pending |
| ARCH-002 | Define configuration system | Pending |
| ARCH-003 | Define logging/metrics conventions | Pending |
| ARCH-004 | Define storage generation/version conventions | Pending |
| ARCH-005 | Define public subsystem interfaces | Pending |

## URL identity

| ID | Task | Status |
|---|---|---|
| URL-001 | Implement URL parser | Pending |
| URL-002 | Implement canonicalizer v1 | Pending |
| URL-003 | Implement BLAKE3-128 UrlId | Pending |
| URL-004 | Implement collision verification | Pending |
| URL-005 | Add canonicalization property tests | Pending |

## Mutable crawl state

| ID | Task | Status |
|---|---|---|
| STATE-001 | Initialize SQLite/WAL | Pending |
| STATE-002 | CandidateRepository | Pending |
| STATE-003 | LivenessRepository | Pending |
| STATE-004 | FrontierRepository | Pending |
| STATE-005 | HostRepository | Pending |
| STATE-006 | IngestionProgressRepository | Pending |

## Common Crawl

| ID | Task | Status |
|---|---|---|
| CC-001 | Discover available crawl metadata | Pending |
| CC-002 | Build crawl/partition planner | Pending |
| CC-003 | Implement streaming partition reader | Pending |
| CC-004 | Add resumable progress tracking | Pending |
| CC-005 | Integrate canonicalization/UrlId | Pending |
| CC-006 | Preserve provenance | Pending |
| CC-007 | Add ingestion statistics | Pending |
| CC-008 | Validate idempotent rerun | Pending |

## Liveness

| ID | Task | Status |
|---|---|---|
| LIVE-001 | DNS resolver | Pending |
| LIVE-002 | DNS cache | Pending |
| LIVE-003 | HTTP/TLS probe | Pending |
| LIVE-004 | State classifier | Pending |
| LIVE-005 | Retry/backoff | Pending |
| LIVE-006 | Liveness benchmarks | Pending |

## Crawler

| ID | Task | Status |
|---|---|---|
| CRAWL-001 | Priority frontier | Pending |
| CRAWL-002 | Host politeness | Pending |
| CRAWL-003 | robots.txt support | Pending |
| CRAWL-004 | Safe HTTP fetcher | Pending |
| CRAWL-005 | HTML parser | Pending |
| CRAWL-006 | Main-text extraction | Pending |
| CRAWL-007 | Outgoing-link extraction | Pending |
| CRAWL-008 | Sitemap discovery | Pending |
| CRAWL-009 | Data-based language detection | Pending |
| CRAWL-010 | SimHash fingerprinting | Pending |

## Shards + manifest

| ID | Task | Status |
|---|---|---|
| STORE-001 | Define shard envelope | Pending |
| STORE-002 | Checksum/signature metadata | Pending |
| STORE-003 | Atomic shard publication | Pending |
| STORE-004 | Manifest schema | Pending |
| STORE-005 | Active-generation switch | Pending |
| STORE-006 | Corruption handling | Pending |

## Lexical index

| ID | Task | Status |
|---|---|---|
| INDEX-001 | Define SearchIndex interface | Pending |
| INDEX-002 | Integrate Tantivy Python path | Pending |
| INDEX-003 | Define indexed fields | Pending |
| INDEX-004 | Implement BM25/BM25F baseline | Pending |
| INDEX-005 | Implement local top-K | Pending |

## Critical routing experiment

| ID | Task | Status |
|---|---|---|
| SPIKE-001 | Build sample evaluation corpus | Pending |
| SPIKE-002 | Build query/relevance set | Pending |
| SPIKE-003 | Implement exhaustive oracle | Pending |
| SPIKE-004 | Test hash shard assignment | Pending |
| SPIKE-005 | Test topic/locality assignment | Pending |
| SPIKE-006 | Test hybrid assignment | Pending |
| SPIKE-007 | Measure routing Recall@K | Pending |
| SPIKE-008 | Measure top-K containment ≤10 shards | Pending |
| SPIKE-009 | Benchmark shard size variants | Pending |
| SPIKE-010 | Benchmark active-shard limits | Pending |

## Routing + cache

| ID | Task | Status |
|---|---|---|
| META-001 | Define routing metadata schema | Pending |
| META-002 | Implement sorted/range presence data | Pending |
| META-003 | Implement Bloom presence filter | Pending |
| META-004 | Versioned flat metadata writer | Pending |
| PLAN-001 | Query analyzer | Pending |
| PLAN-002 | Candidate shard scorer | Pending |
| PLAN-003 | Load-order planner | Pending |
| PLAN-004 | Early stopping | Pending |
| CACHE-001 | L1 cache | Pending |
| CACHE-002 | L2 shard cache | Pending |
| CACHE-003 | Frequency-aware eviction | Pending |
| CACHE-004 | Byte accountant | Pending |

## Graph

| ID | Task | Status |
|---|---|---|
| GRAPH-001 | UrlId↔ordinal mapping | Pending |
| GRAPH-002 | Hyperlink graph writer | Pending |
| GRAPH-003 | Graph shard reader | Pending |
| GRAPH-004 | Domain graph derivation | Pending |
| GRAPH-005 | PageRank implementation | Pending |
| GRAPH-006 | PageRank shard publication | Pending |

## Ranking

| ID | Task | Status |
|---|---|---|
| RANK-001 | Feature schema | Pending |
| RANK-002 | Linear baseline ranker | Pending |
| RANK-003 | Freshness feature | Pending |
| RANK-004 | Global authority feature | Pending |
| RANK-005 | Topic heuristic | Pending |
| RANK-006 | Domain diversity | Pending |
| RANK-007 | Explain ranking output | Pending |

## API

| ID | Task | Status |
|---|---|---|
| API-001 | Health/version endpoints | Pending |
| API-002 | Search request schema | Pending |
| API-003 | Search endpoint | Pending |
| API-004 | Pagination | Pending |
| API-005 | Domain/freshness filters | Pending |
| API-006 | Explain mode | Pending |

## Security

| ID | Task | Status |
|---|---|---|
| SEC-001 | Ed25519 signature verification | Pending |
| SEC-002 | Pinned root key mechanism | Pending |
| SEC-003 | Artifact checksum verification | Pending |
| SEC-004 | Safe serialization policy enforcement | Pending |
| SEC-005 | Crawler SSRF protections | Pending |

## Evaluation

| ID | Task | Status |
|---|---|---|
| EVAL-001 | Retrieval metric runner | Pending |
| EVAL-002 | Routing metric runner | Pending |
| EVAL-003 | Performance benchmark harness | Pending |
| EVAL-004 | Cache benchmark | Pending |
| EVAL-005 | PageRank validation | Pending |

## Feedback/community

| ID | Task | Status |
|---|---|---|
| FB-001 | Local feedback event schema | Pending |
| FB-002 | Privacy-preserving aggregation | Pending |
| FB-003 | Local ranking delta | Pending |
| COMM-001 | Signed contribution format | Pending |
| COMM-002 | Git contribution workflow | Pending |
| COMM-003 | Validation pipeline | Pending |
| COMM-004 | Global metadata merge | Pending |
| COMM-005 | Global ranking release | Pending |

---

# 50. Critical Go/No-Go Gate

Before scaling ingestion aggressively, the system must prove the central routing hypothesis.

Do not ingest/build a massive corpus and then discover the shard model fails.

Required experiment:

```text
sample corpus
    ↓
build alternative shard assignments
    ↓
exhaustive search oracle
    ↓
metadata routing
    ↓
measure top-K containment
```

Primary question:

> For real representative queries, what percentage of queries recover the true top-K candidates while opening no more than the configured shard budget?

If this is poor, redesign shard assignment/routing before scaling.

---

# 51. Definition of MVP

MVP is complete when all of the following work end-to-end on CPU:

```text
Common Crawl sample
   ↓
candidate URL ingestion
   ↓
canonicalization + UrlId
   ↓
SQLite mutable state
   ↓
liveness
   ↓
crawl
   ↓
parse
   ↓
shard
   ↓
index
   ↓
metadata routing
   ↓
query planner
   ↓
BM25 retrieval
   ↓
PageRank feature
   ↓
multi-signal ranking
   ↓
REST search API
```

Additionally:

- restart works
- corruption detection works
- memory remains bounded
- large files obey shard policy
- routing metrics are measurable
- benchmarks exist
- results can be explained
- no GPU is required

---

# 52. Explicit Non-Goals for MVP

Do not block MVP on:

- neural semantic search
- distributed cluster operation
- hosted community aggregation
- sophisticated federated learning
- huge LTR models
- Kubernetes
- GPU acceleration
- real-time web-wide freshness
- perfect spam detection
- full web coverage
- Google-scale crawling

---

# 53. Coder Agent Instructions

When implementing:

1. Follow this document as the architecture contract.
2. Do not redesign major subsystems without documenting the reason.
3. Never silently violate the bounded-memory principles.
4. Keep all large operations streaming.
5. Use interfaces around replaceable components.
6. Keep the serving process read-mostly.
7. Keep mutable crawl state in SQLite/WAL.
8. Never use 64-bit UrlId.
9. Never introduce a mandatory GPU dependency.
10. Never introduce another project implementation language.
11. Never deserialize untrusted executable Python objects.
12. Do not mark a task `done` until tests pass.
13. If a design assumption fails in benchmark/evaluation, update architecture before scaling.
14. Prefer simple measurable algorithms before complex speculative ones.
15. Record every benchmark and major decision.

---

# 54. First Implementation Milestone

Build the smallest useful vertical slice:

```text
1. canonicalize URL
2. generate UrlId
3. save candidate to SQLite
4. ingest a tiny Common Crawl sample
5. liveness-check selected URLs
6. crawl a small safe set
7. parse title/text/links
8. build one small lexical index
9. build minimal routing metadata
10. query through API
```

Do not begin with billion-scale ingestion.

Once this path works, expand progressively.

---

# 55. Final Architecture

```text
                            PUBLIC WEB
                                │
                    ┌───────────┴───────────┐
                    │                       │
             COMMON CRAWL              DISCOVERED LINKS
                    │                       │
                    └───────────┬───────────┘
                                ▼
                         URL DISCOVERY
                                │
                                ▼
                       CANONICALIZATION
                                │
                                ▼
                         BLAKE3-128 UrlId
                                │
                                ▼
                        SQLITE/WAL STATE
                                │
                                ▼
                       DNS / LIVENESS
                                │
                                ▼
                             CRAWLER
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
               CONTENT                      LINKS
                  │                           │
                  ▼                           ▼
             URL/DOC SHARDS             WEB GRAPH
                  │                           │
                  ▼                           ▼
             LEXICAL INDEX                PAGERANK
                  │                           │
                  └─────────────┬─────────────┘
                                ▼
                     ROUTING METADATA
                         range + Bloom
                                │
                                ▼
                         MASTER MANIFEST
                                │
                                ▼
                              QUERY
                                │
                                ▼
                         QUERY PLANNER
                                │
                     choose best shard(s)
                                │
                      bounded shard cache
                                │
                                ▼
                         BM25 RETRIEVAL
                                │
                           local top-K
                                │
                                ▼
                         GLOBAL MERGE
                                │
                                ▼
                        RANKING FEATURES
                   ┌────────────┼────────────┐
                   ▼            ▼            ▼
                PageRank     freshness     quality
                   │            │            │
                   └────────────┼────────────┘
                                ▼
                         CPU FINAL RANKER
                                │
                                ▼
                              TOP-K
                                │
                                ▼
                           SEARCH API
                                │
                                ▼
                              AGENT
```

Community loop:

```text
local usage
    ↓
local aggregate observations
    ↓
signed contribution
    ↓
Git/community validation
    ↓
new metadata/ranking release
    ↓
signed manifest/artifacts
    ↓
local installations
```

---

# 56. The Four Rules That Must Never Be Lost

## Rule 1

> The corpus can be enormous; the runtime working set must remain small.

## Rule 2

> Metadata decides where to search; normal queries never scan the entire corpus.

## Rule 3

> PageRank is a ranking signal, not the search algorithm.

## Rule 4

> CPU-only Python operation is the baseline, not a fallback.

---

# 57. Final Note to the Implementer

The highest-risk part of this project is not the crawler, PageRank, or the API.

It is the assumption that routing metadata and shard assignment can consistently place/retrieve relevant candidates within a small shard budget.

Treat this as a hypothesis to validate early.

Do not spend months scaling the corpus until the routing experiment proves that the architecture can retrieve high-quality candidates under the bounded working-set constraints.

Everything else can be improved incrementally once that foundation is demonstrated.
