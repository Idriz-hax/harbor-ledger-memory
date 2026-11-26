# Performance baseline

This is a point-in-time reference, not a performance guarantee. The startup
measurements below are from the local development checkout and include its
uncommitted backend changes; they should not be treated as a clean-release
baseline. No real vault data was read.

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

Started the actual `hlm serve` process three times, each with an isolated
temporary `HOME`, SQLite database, and 500 short synthetic Markdown notes.
Embedding was disabled. “Full scan ready” means the initial scan completed,
not merely that the HTTP server began accepting requests. Idle samples were
taken for 8 seconds after scan completion.

| Measurement | Result |
| --- | ---: |
| `/health` available, p50 (n=3) | 693 ms (674–1,368 ms) |
| Initial scan complete / service ready, p50 (n=3) | 908 ms (892–1,587 ms) |
| Idle RSS, median across trials | 113.6 MiB |
| Idle CPU, approximate process average over 8 seconds | 0.25% |

The first process launch was slower than subsequent launches; these three
trials are too few to characterize cold-machine variance.

### Synthetic full-scan throughput

Each trial ran `ScanService.full_scan()` in a fresh Python process against a
temporary vault with short notes and no links or embeddings. Peak RSS is the
median maximum resident set size across three fresh processes.

| Synthetic notes | Scan p50 (n=3) | Throughput p50 | Peak RSS p50 |
| ---: | ---: | ---: | ---: |
| 500 | 0.346 s | 1,445 notes/s | 77.3 MiB |
| 2,000 | 1.122 s | 1,783 notes/s | 92.0 MiB |

### Fresh lexical query latency

Each run created and scanned a fresh synthetic catalog, then made one initial
query followed by 20 warmed `QueryService.query()` calls in the same process.
The table reports medians across three processes. Embeddings were disabled.

| Synthetic notes | First query p50 | Warm query p50 | Warm query p95 |
| ---: | ---: | ---: | ---: |
| 500 | 62.9 ms | 57.4 ms | 70.1 ms |
| 2,000 | 211.1 ms | 199.0 ms | 207.5 ms |

This is a direct service query with local SQLite, not HTTP transport. The
optional `sentence-transformers` package is not installed in the benchmark
environment, so semantic query timing was not rerun on this worktree.

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

The runtime environment size is not directly comparable with the developer
`.venv`: the latter includes development and build tooling. The wheel is pure
Python and does not bundle third-party dependencies.

Keep any real-vault measurements local. Record aggregate metrics only; do not
include note contents or sensitive derived details in reports or send them
outside this environment.
