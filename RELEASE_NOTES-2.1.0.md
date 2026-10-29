# Harbor Ledger Memory 2.1.0

## Multilingual semantic search

- Embeddings remain optional; the default model setting remains disabled. Without the `embeddings` extra or a configured model, lexical and graph retrieval continue to work.
- E5 model identifiers (including `intfloat/multilingual-e5-small`) now receive model-specific `passage: ` prefixes for indexed notes and `query: ` prefixes for search queries. Non-E5 models such as `all-MiniLM-L6-v2` receive unchanged text.
- Install/configure an optional model and rebuild its vectors:

  ```text
  uv tool install --reinstall '.[embeddings]'
  hlm config set --embedding-model intfloat/multilingual-e5-small
  hlm scan
  ```

  Model weights download on first use into the local cache, not during installation. `all-MiniLM-L6-v2` is a compact English baseline, not a model this project has established as best. Run `hlm scan` whenever the model ID changes; vectors are model-keyed and Markdown is not modified.

## Explicit note reads

Search first, then read selected paths using `read_note`/`read_notes` or the CLI. Batch reads accept up to 10 explicit paths, return independent per-path results, and never automatically expand search results.

## Performance

The README includes a concise HLM search comparison against embeddings-disabled
search. Detailed synthetic model timings, methodology, and catalog-size scoring
measurements are in the [embedding benchmark](docs/embedding-benchmark.md).
Results are machine- and corpus-specific, not general speed or retrieval-quality
guarantees; no private vault content was used.
