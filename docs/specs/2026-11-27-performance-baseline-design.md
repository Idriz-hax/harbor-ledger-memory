# Performance and footprint baseline design

## Goal

Make Harbor Ledger Memory lighter and faster based on evidence, while leaving
the UI and current behavior unchanged during the baseline phase.

## Approach

Establish repeatable measurements before choosing an optimization. Cover cold
startup and embedding-model initialization, idle CPU and resident memory,
lexical and semantic query latency at multiple catalog sizes, scan throughput
and peak memory, and installed artifact/dependency footprint. Report p50/p95
where repeated samples make those statistics useful. Record hardware, software
versions, commands, and benchmark dataset size so later measurements are
comparable. Reuse existing synthetic benchmark conventions where practical.

## Data handling

Synthetic data is preferred for repeatable size sweeps. Real vault data may be
used for local measurements if needed to reflect actual workloads. Do not send
vault contents to external services, check them into the repository, or publish
note contents or derived sensitive details. Reports contain aggregate metrics
only; any local real-vault measurements stay local and are not included in
shared artifacts.

## Scope and output

This phase adds or updates only the minimal benchmark tooling and aggregate
documentation needed to establish the baseline. It does not change UI, search
semantics, runtime behavior, dependencies, or performance implementation. The
result is a local, reproducible benchmark procedure and a baseline report that
can guide selection of one evidence-backed optimization in a later phase.

## Validation

Run the benchmark on synthetic fixtures and verify output contains environment
and aggregate timing/resource/footprint data but no note text. If real vault
data is used, confirm its contents and sensitive derived details remain local.
Run focused tests for any benchmark code added; do not run full application
benchmarks against the user's vault without explicit confirmation at execution
time.

## Constraints

- Preserve existing uncommitted changes in the working tree.
- Keep real vault data local; do not publish or transmit it outside this
  environment.
- Make no speculative optimizations during baseline collection.
