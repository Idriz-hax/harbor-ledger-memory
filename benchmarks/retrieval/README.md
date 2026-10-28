# Retrieval benchmark

The optional private-vault benchmark is local-only. Keep cases and results in
`local/` (ignored by git); only the safe `scope.example.json` placeholder is
tracked. Validators inspect input files only and never read vault contents.
Do not put note bodies or excerpts in benchmark data. The synthetic benchmark
below remains the portable regression suite.

The synthetic JSONL cases are split into two public regression groups:

- `dev`: cases used while tuning retrieval.
- `heldout`: public cases used only for regression reporting after tuning.

Both groups contain every benchmark category. `heldout` is not a private canary
or a claim about unseen/private data.

Run the lexical candidate against the hash-bound baseline:

```text
uv run python -m benchmarks.retrieval.runner --compare-baseline
```

The command reports baseline and candidate metrics per split. Ranking metrics
exclude negative/ambiguous cases; negative accuracy is calculated only on those
cases. Exact selected-path equality is checked between repeated candidate runs
for determinism. Regenerate the baseline only after an intentional reviewed
change:

```text
uv run python -m benchmarks.retrieval.runner --write-baseline
```

## Private local-vault benchmark

Create `local/scope.json` and `local/cases.jsonl` from the safe scope template;
both files and `local/baseline.json` are ignored. Defaults never write outside
`local/`.

```text
uv run python -m benchmarks.retrieval.private_vault --dry-run
uv run python -m benchmarks.retrieval.private_vault --write-baseline
uv run python -m benchmarks.retrieval.private_vault --write-baseline --confirm-replace
uv run python -m benchmarks.retrieval.private_vault --compare-baseline
```

Dry-run validates labels and reports counts/hashes without queries. Runs read
admitted notes into a disposable local catalog; no text is sent to hosted
services. Query text and note paths are sensitive. Results omit them by default;
`--show-private-details` exposes paths locally, never note contents. A clean
retrieval-only comparison requires unchanged case, scope, and corpus hashes;
corpus drift is reported separately and is not a retrieval improvement claim.
