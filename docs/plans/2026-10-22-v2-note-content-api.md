# HLM Note Content API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded, authenticated read-by-path API so native memory providers can retrieve full Markdown notes without bypassing HLM policy.

**Architecture:** Add one REST endpoint backed by `VaultBoundary` snapshots and the caller's existing `AccessPolicy`. Canonicalize one vault-relative identity, authorize that identity, then read it through the no-follow boundary; never read through arbitrary `Path` operations.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, `VaultBoundary`, pytest.

---

## File map

- Modify `backend/src/harbor_ledger_memory/vault/boundary.py`: expose one validated identity method and enforce a byte ceiling before allocating snapshot content.
- Modify `backend/src/harbor_ledger_memory/api/app.py`: response model and `GET /api/v1/notes/content?path=AI%2FProjects%2FExample.md`.
- Create `tests/test_note_content_api.py`; extend `tests/test_vault_boundary.py` for size-limited snapshots.
- The final v2 release plan updates root `README.md` and `CHANGELOG.md` after both native providers are integrated.

## Task 1: Specify failing API and boundary tests

- [x] Add tests for an authorized UTF-8 Markdown read returning exact content, vault-relative canonical path, and SHA-256 revision; assert the read works before a catalog rescan.
- [x] Add tests for missing/denied paths returning indistinguishable 404, invalid credentials returning 401, malformed/non-Markdown paths returning 422, nested URL escapes, and over-limit files returning 413.
- [x] Add boundary tests proving the byte ceiling is checked before reading, including a file that grows during read; preserve existing symlink/race tests.
- [x] Run `uv run pytest tests/test_note_content_api.py tests/test_vault_boundary_writes.py tests/test_token_auth.py -q`; confirm the new route/tests fail before implementation.

Example API assertion:

```python
response = client.get(
    "/api/v1/notes/content",
    params={"path": "AI/Projects/Café Notes.md"},
    headers=_headers(reader_token),
)
assert response.status_code == 200
assert response.json()["content"] == expected_content
assert response.json()["revision"] == hashlib.sha256(expected_content.encode()).hexdigest()
```

## Task 2: Implement bounded boundary snapshots and route

- [x] Add public `VaultBoundary.validate_identity(relative) -> PurePosixPath`, delegating to `_validated_identity` and rejecting residual percent-encoded escapes so later validation cannot change the canonical path.
- [x] Add optional `max_bytes` to `read_file` / `_read_snapshot`; reject `st_size` above the limit before `stream.read`, read at most `max_bytes + 1`, and recheck descriptor metadata so concurrent growth fails explicitly. Raise a distinct `VaultFileTooLarge` error.
- [x] Define a fixed 1 MiB note-content limit and `NoteContentResponse(path, content, revision)` in `api/app.py`.
- [x] In the route, require authenticated access, `.md` identity, global admission and `auth.policy.can_read(canonical_path)`. Return 404 for both missing and unreadable identities, 422 for malformed paths/invalid UTF-8, 413 for oversized notes; serialize UTF-8 content and snapshot SHA-256 and set `Cache-Control: no-store`.
- [x] Add no admin/localhost bypass, no URL re-decoding after canonicalization, and no truncated success response.

The route uses the same canonical identity for both policy and safe read:

```python
identity = boundary.validate_identity(path)
if identity.suffix.lower() != ".md" or not auth.policy.can_read(identity.as_posix()):
    raise HTTPException(status_code=404, detail="note unavailable")
snapshot = boundary.read_file(identity, max_bytes=MAX_NOTE_BYTES)
return NoteContentResponse(
    path=identity.as_posix(),
    content=snapshot.content.decode("utf-8"),
    revision=snapshot.sha256,
)
```

## Task 3: Verify, document, and commit

- [x] Run `uv run pytest tests/test_note_content_api.py tests/test_vault_boundary_writes.py tests/test_token_auth.py -q` and `uv run pyright backend/src`.
- [x] Run `uv run ruff check --extend-ignore E501 backend/src/harbor_ledger_memory/api/app.py backend/src/harbor_ledger_memory/api/auth.py backend/src/harbor_ledger_memory/vault/boundary.py tests/test_note_content_api.py tests/test_vault_boundary_writes.py` and `uv run ruff format --check` on the touched files. The endpoint and boundary tests passed; `app.py` retains unrelated pre-existing formatter differences.
- [ ] Commit the API implementation and tests as `feat: add policy-checked note content API`.

**Verification record:** Focused API/boundary/auth tests passed (96). Pyright still reports seven pre-existing `reportUnusedFunction` errors also present on `main`; there are no new diagnostics for `note_content`. `ruff check` passed with the existing long-line rule ignored for this targeted run; formatting-only changes to unrelated `app.py` lines were intentionally avoided.
