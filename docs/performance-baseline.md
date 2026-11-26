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

## Not measured yet

- Service-ready startup and idle CPU/RSS.
- Scan throughput and peak RSS at representative vault sizes.
- Installed wheel/artifact size and dependency footprint separate from `.venv`.
- Fresh end-to-end query measurements on this exact worktree.

Keep real-vault measurements local. If performed later, record aggregate
metrics only; do not include note contents or sensitive derived details in
reports or send them outside this environment.
