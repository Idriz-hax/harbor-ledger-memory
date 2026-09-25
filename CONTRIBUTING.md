# Contributing

Thanks for helping improve Harbor Ledger Memory. Please keep changes focused,
document user-visible behavior, and avoid committing vault data, credentials, or
generated output.

## Development setup

The project requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and
Node.js 18+ for the bundled web UI:

```text
uv sync
uv sync --extra embeddings  # optional; downloads the embedding stack
(cd web && npm ci && npm run build)
```

## Checks

Run the relevant checks before opening a pull request:

```text
uv run pytest -v
uv run alembic -c backend/alembic.ini upgrade head
uv run ruff check backend tests
uv run ruff format --check .
uv run pyright backend/src
uv run python -m benchmarks.retrieval.runner --compare-baseline
uv run hlm doctor --json
```

`hlm doctor` is safe to run in clean environments: it performs inspection only,
does not create configuration/catalog files, and does not download embedding
models. Use `hlm doctor --url http://127.0.0.1:8765` only for an explicit local
health/OpenAPI probe; never include credentials in the URL.

The web build is also part of the setup command above. If you change the web
application, run `(cd web && npm test -- --run)` as well.

## Pull requests

- Explain the problem and the approach in the pull request description.
- Include tests or a clear reason tests are not applicable.
- Update `README.md` or `CHANGELOG.md` when the documented or released surface
  changes.
- Keep security-sensitive details out of public issues; see [the security
  policy](SECURITY.md).
