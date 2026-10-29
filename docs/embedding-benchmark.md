# Embedding benchmark

These are small synthetic-only measurements, not performance guarantees or a
general ranking of model quality. No private vault content was read.

## Setup

Tests ran on macOS 26.6.2 arm64 with Python 3.12.13, PyTorch 2.15.0, and MPS
(`mps:0`). The model-only comparison used cached weights in a fresh process per
model. The English corpus contained 10 target notes and 190 unrelated English
distractors, with 10 paraphrased queries. The multilingual corpus contained 10
English target notes and 190 unrelated distractors in English, Spanish, French,
and German; its 20 queries were 10 English and 10 Spanish, each expected to
retrieve an English note. Notes were short; long-note truncation was not tested.

Batch indexing is the median of three runs over 200 notes. Query encoding and
encode-plus-rank timings are warmed p50/p95 across the 10 or 20 queries. The
rank step scores and sorts 200 precomputed vectors; it excludes SQLite loading
and the rest of HLM retrieval. Peak RSS is for the entire Python/PyTorch/model
process, not the model alone. Cached-weight load times exclude model downloads.

## Model-only comparison

All models produced 384-dimensional vectors.

### English models

| Model (max tokens) | Cached-weight load | Batch 200 notes | Query encode p50 / p95 | Encode + rank p50 / p95 | Peak RSS | Top-1 / MRR@10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `sentence-transformers/all-MiniLM-L6-v2` (256) | 4.911 s | 0.111 s | 5.973 / 7.234 ms | 10.857 / 12.115 ms | 452.8 MiB | 10/10 / 1.00 |
| `intfloat/e5-small-v2` (512) | 4.009 s | 0.123 s | 7.181 / 8.104 ms | 11.569 / 12.593 ms | 511.6 MiB | 10/10 / 1.00 |

Both models retrieved the expected result for all 10 queries. MiniLM was
slightly faster and used less process memory in this run; this sample is too
small to conclude either model is more accurate generally.

### Multilingual models

| Model (max tokens) | Cached-weight load | Batch 200 notes | Query encode p50 / p95 | Encode + rank p50 / p95 | Peak RSS | Top-1 / MRR@10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `intfloat/multilingual-e5-small` (512) | 3.852 s | 0.171 s | 6.728 / 7.418 ms | 10.931 / 12.252 ms | 898.7 MiB | 19/20 / 0.95 |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (128) | 4.370 s | 0.152 s | 7.052 / 9.385 ms | 11.436 / 14.035 ms | 822.9 MiB | 20/20 / 1.00 |

The multilingual MiniLM had a lower measured process RSS and one additional
top-1 hit; its 128-token limit may truncate longer notes. Multilingual E5 has a
512-token limit and slightly faster query encoding in this run. HLM applies E5
`query: ` and `passage: ` prefixes automatically.

## HLM search with and without embeddings

Each setting used the same synthetic catalog and queries in a fresh SQLite
database, with identical query settings, no active project or user scope, and
no linked notes. Timings are warm `QueryService.query` calls after model load
and vector generation. They include HLM retrieval, activation, and context
selection, but exclude transport and the one-time model download.

The English set had 200 notes (10 target topics and 190 distractors) and 20
queries (10 direct and 10 paraphrased). The multilingual set had 200 notes (10
English target notes and 190 unrelated notes across four languages) and 40
queries: 10 direct and 10 paraphrased in English and Spanish, all targeting
English notes.

### English search

| Configuration | Direct top-1 | Paraphrase top-1 | Overall top-1 | Recall@5 | MRR@10 | Query p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| No embeddings | 6/10 | 0/10 | 6/20 | 6/20 | 0.300 | 2.4 / 18.2 ms |
| `all-MiniLM-L6-v2` | 10/10 | 10/10 | 20/20 | 20/20 | 1.000 | 34.7 / 40.1 ms |
| `intfloat/e5-small-v2` | 10/10 | 10/10 | 20/20 | 20/20 | 1.000 | 37.0 / 41.4 ms |

### Multilingual search (Spanish queries target English notes)

| Configuration | EN direct | EN paraphrase | ES direct | ES paraphrase | Overall top-1 | Recall@5 | MRR@10 | Query p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No embeddings | 6/10 | 0/10 | 0/10 | 0/10 | 6/40 | 6/40 | 0.150 | 2.3 / 18.0 ms |
| `intfloat/multilingual-e5-small` | 10/10 | 9/10 | 5/10 | 5/10 | 29/40 | 30/40 | 0.738 | 36.8 / 38.8 ms |
| `paraphrase-multilingual-MiniLM-L12-v2` | 10/10 | 10/10 | 5/10 | 7/10 | 32/40 | 32/40 | 0.800 | 36.9 / 39.3 ms |

Both English models improved paraphrase retrieval against this baseline. Both
multilingual models improved cross-language retrieval; multilingual MiniLM
ranked more Spanish queries correctly in this sample. The results are too
small and synthetic to establish a general quality winner.

## Catalog-size scoring cost

The current semantic retrieval implementation loads and scores every stored
embedding per query. An isolated sweep of HLM's 384-dimensional Python cosine
helper over random vectors (10 repeats) measured the scoring loop only; it
excludes database loading and the rest of retrieval.

| Stored vectors | Cosine-scan median | p95 |
| ---: | ---: | ---: |
| 200 | 6.2 ms | 6.2 ms |
| 1,000 | 30.7 ms | 31.0 ms |
| 5,000 | 151.9 ms | 154.5 ms |
| 10,000 | 306.8 ms | 312.0 ms |

This linear scan is model-independent and may dominate query time in large
catalogs. End-to-end latency will be higher than these isolated scan timings.
