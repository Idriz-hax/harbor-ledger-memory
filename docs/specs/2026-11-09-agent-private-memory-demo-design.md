# Agent-private memory CLI demo design

**Status:** Proposed for review

**Date:** 2026-11-09

## Goal

Provide a repeatable way to demonstrate agent-private memory isolation without
installing or configuring Hermes. The demo must exercise HLM's real REST
authentication and access-policy path while remaining isolated from the user's
configured vault and database.

## Proposed command

Add `hlm demo agent-private`. It creates a temporary vault and SQLite catalog,
enables agent-private memory, creates two tokens bound to distinct principals,
and runs requests against an in-process HLM application. It prints a concise,
human-readable walkthrough of the checks and their outcomes, then removes the
temporary data.

The demo will verify that:

1. Both tokens can read a shared note.
2. Each token can read its own private note.
3. Each token is denied access to the other principal's private note.
4. With agent-private memory disabled, neither token can read a note under the
   reserved private subtree.

The command exits successfully only when every expected result is observed;
unexpected responses produce a non-zero exit and a useful error message.

## Isolation and safety

- Use a fresh temporary directory for the vault and catalog; never load the
  user's normal HLM settings or write to their configured vault/database.
- Do not start a network listener or call an external service.
- Do not print plaintext bearer tokens or store them outside the temporary
  process state.
- Clean up temporary files on success and failure.
- Do not modify the running server's feature switch or token configuration.

## Implementation boundary

The demo belongs in the existing Typer CLI. Reuse the production app factory,
token service, and REST endpoints rather than reproducing authorization logic
inside the demo. Keep it as a diagnostic walkthrough, not a permanent demo
server, browser page, or Hermes integration.

## Verification

- Add CLI coverage for the successful walkthrough, cleanup, and a failed
  assertion path.
- Verify the command does not consult ambient vault/database configuration.
- Run the focused CLI and REST auth tests, then the full Python test suite.

## Out of scope

- Hermes installation, profile setup, or provider configuration.
- A browser-based demo or a long-running demo server.
- Mutating real vault data or existing tokens.
