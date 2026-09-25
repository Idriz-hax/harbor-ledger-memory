# Retrieval benchmark

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
