# Performance baseline

This is a point-in-time reference, not a performance guarantee. The startup
measurements below are from the local development checkout and include its
uncommitted backend changes; they should not be treated as a clean-release
baseline. No real vault data was read. The app's configured vault and index
root were unavailable in this environment, so the application-scale tests use
synthetic vaults of 500 and 2,000 short Markdown notes.

## Local import and footprint

Measured on 2026-11-27, macOS 26.6.2 arm64, Python 3.12.13, using the existing
`.venv`. Seven fresh Python processes imported `harbor_ledger_memory.api.app`;
elapsed wall-clock time includes interpreter startup and module import.

| Measurement | Result |
| --- | ---: |
| Cold process + API module import, p50 (n=7) | 431 ms |
| Cold process + API module import, range | 392–774 ms |
| Maximum RSS for API module import, median (n=3) | 91.8 MB |
| Development virtual environment on disk | 150,860 KiB (about 147.3 MiB) |
| Backend Python package source on disk | 1,832 KiB (about 1.8 MiB) |

These are not service-ready startup or idle-service measurements: the import
does not start the app, watcher, or embedding model. The virtual environment is
the developer environment, not an installed release artifact.

## Existing retrieval reference

The synthetic retrieval and embedding measurements in
[`embedding-benchmark.md`](embedding-benchmark.md) remain the available query
baseline. In that benchmark, with 200 synthetic notes, lexical-only end-to-end
query latency was 2.4 ms p50 / 18.2 ms p95, and MiniLM semantic query latency
was 34.7 ms p50 / 40.1 ms p95. These were previously measured results, not
rerun during this baseline capture; see that report for methodology and
limitations.

## Additional local measurements

Measured on the same machine and checkout on 2026-11-27. These measurements
use temporary synthetic vaults and databases only; no real vault data was
read. The checkout still contained pre-existing uncommitted backend changes,
so these are reference measurements for this worktree, not a clean-release
comparison.

### Service startup and idle resources

Started the actual `hlm serve` process ten times, each with an isolated
temporary `HOME`, SQLite database, and 500 short synthetic Markdown notes.
Embedding was disabled. “Scan ready” means the initial scan completed, not
merely that the HTTP server began accepting requests. RSS and CPU were sampled
for 8 seconds after scan completion. CPU is approximate because macOS process
CPU-time reporting has coarse resolution over this interval.

| Measurement | p50 | p95 | Range |
| --- | ---: | ---: | ---: |
| `/health` available (n=10) | 786 ms | 1,020 ms | 604–1,062 ms |
| Initial scan complete / service ready (n=10) | 1,072 ms | 1,261 ms | 850–1,369 ms |
| Idle RSS (per-trial median, n=10) | 105.2 MiB | 113.1 MiB | 39.1–113.2 MiB |
| Idle CPU average (per-trial, n=10) | 0.56% | 0.75% | 0.25–0.88% |

These were ten fresh processes on the same machine; operating-system caches
were not cleared between trials.

### Synthetic full-scan throughput

Each trial ran `ScanService.full_scan()` in a fresh Python process against a
temporary vault with short notes and no links. Ten trials were run at each
size. Peak RSS is the median maximum resident set size across the ten
processes. Both lexical-only and embedding-enabled scans were measured; the
latter used the local `all-MiniLM-L6-v2` model cache with network access
disabled.

| Mode / notes | Scan p50 | Scan p95 | Throughput p50 | Peak RSS p50 (max) |
| --- | ---: | ---: | ---: | ---: |
| Lexical / 500 | 0.345 s | 0.384 s | 1,447 notes/s | 77.3 MiB (77.3) |
| Lexical / 2,000 | 1.141 s | 1.170 s | 1,754 notes/s | 92.0 MiB (92.2) |
| MiniLM / 500 | 3.215 s | 3.636 s | 156 notes/s | 568.0 MiB (571.7) |
| MiniLM / 2,000 | 4.577 s | 4.688 s | 437 notes/s | 617.2 MiB (617.5) |

### Fresh lexical query latency

Each run created and scanned a fresh synthetic catalog, then made one initial
query followed by 30 warmed `QueryService.query()` calls in the same process.
Ten fresh processes were run at each size. Embeddings were disabled. p95/p99
are the medians of each process's warm-query percentile.

| Synthetic notes | First query p50 | Warm p50 | Warm p95 | Warm p99 |
| ---: | ---: | ---: | ---: | ---: |
| 500 | 65.0 ms | 59.5 ms | 72.2 ms | 76.3 ms |
| 2,000 | 220.8 ms | 204.1 ms | 236.1 ms | 250.9 ms |

This is a direct service query with local SQLite, not HTTP transport. Semantic
query measurements follow, using the optional embedding runtime.

### Semantic model and query cost

The embeddings extra was installed into a separate, clean environment; no
dependencies were added to the checkout's `.venv`. The tested stack was Python
3.12.13, PyTorch 2.13.0, sentence-transformers 5.7.0, and Transformers 5.14.1
on Apple MPS (`mps:0`). Hub access was disabled; the pre-cached MiniLM weights
occupied 89,480 KiB on disk. Ten fresh processes measured model encode/load
with cached weights; ten query trials per catalog each made one initial query
and 30 warm queries.

| Measurement | p50 | p95 | Range |
| --- | ---: | ---: | ---: |
| First model encode in a fresh process (n=10) | 2.74 s | 3.18 s | 2.59–4.27 s |
| First semantic query, 500 notes (n=10) | 2.76 s | 3.08 s | — |
| Warm semantic query, 500 notes | 81.5 ms | 93.3 ms | p99 180.1 ms |
| First semantic query, 2,000 notes (n=10) | 3.62 s | 4.00 s | — |
| Warm semantic query, 2,000 notes | 287.8 ms | 375.9 ms | p99 412.0 ms |
| Semantic-query process peak RSS, 500 notes | 508.6 MiB | — | — |
| Semantic-query process peak RSS, 2,000 notes | 548.9 MiB | — | — |

For the semantic-enabled server, ten fresh CLI processes scanned 500 notes
before scan-ready. `/health` was available at 576 ms p50 / 736 ms p95; the
initial embedding scan completed at 3.14 s p50 / 3.31 s p95 (2.98–4.69 s
range). Idle RSS was 449.3 MiB p50 (394.2–449.6 MiB range), and idle CPU was
approximately 0.25% p50 / 0.63% p95 in five additional 8-second idle samples.
This compared with 1.07 s scan-ready and 105.2 MiB idle RSS with embeddings
disabled. These measurements show startup cost when embeddings are configured,
not a universal default; the model is an optional extra.

### Built artifact and clean runtime footprint

Built the sdist and wheel from this checkout into a temporary directory, then
installed the wheel into a fresh Python 3.12 environment with the locked
runtime dependencies and no development dependencies.

| Measurement | Result |
| --- | ---: |
| Wheel (compressed) | 1,147,109 bytes (1.09 MiB) |
| Source archive (compressed) | 2,008,280 bytes (1.91 MiB) |
| Installed HLM package files | 2,568 KiB |
| Fresh runtime environment site-packages (HLM + 51 runtime packages) | 62,540 KiB (61.1 MiB) |
| Fresh embeddings-enabled site-packages (HLM + 72 runtime packages) | 802,340 KiB (783.5 MiB) |
| Added site-packages footprint for embeddings extra | 739,800 KiB (722.5 MiB) |
| Cached MiniLM model weights (separate from Python install) | 89,480 KiB (87.4 MiB) |

The runtime environment size is not directly comparable with the developer
`.venv`: the latter includes development and build tooling. The wheel is pure
Python and does not bundle third-party dependencies. The embeddings-enabled
footprint includes PyTorch, transformer/runtime libraries, and HLM; model
weights are a separate cache.

## Recommended first optimization

First investigate keeping the optional semantic runtime out of the service's
startup/scan critical path when it is not needed. In this synthetic fixture,
configuring MiniLM increased scan-ready time from 1.07 s to 3.14 s and idle RSS
from 105.2 MiB to 449.3 MiB; the embeddings extra added 722.5 MiB to installed
site-packages plus 87.4 MiB of cached model weights. Keep semantic search
available as an option, but avoid loading/indexing it as a prerequisite for
lexical service readiness if the product behavior can tolerate semantic
results becoming available later. The tradeoff is delayed semantic retrieval;
measure its first-use experience and confirm on a representative real vault
before deciding on the implementation. These synthetic figures justify
investigating this path first, not promising the same gains on every vault.

Keep any real-vault measurements local. Record aggregate metrics only; do not
include note contents or sensitive derived details in reports or send them
outside this environment.

## Coverage limits

- The configured application vault/index root was not available locally, so
  no real-vault scan or query was performed. Synthetic workload sizes are
  representative benchmarks, not a substitute for that vault's note/link
  distribution.
- No concurrent-client/load test, sustained watcher test, power-use test, or
  first-time model download was run. Model measurements use cached local
  weights and no network access.
- All application measurements describe the current dirty checkout and local
  Apple Silicon environment; compare future results on the same machine and
  workload before drawing performance conclusions.
