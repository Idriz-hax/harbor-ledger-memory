# Agent-private scope in Settings and token audit design

**Status:** Approved for planning

**Date:** 2026-11-18

## Goal

Make agent-principal binding visible and usable in the existing Settings token
management UI, and make token lifecycle activity explain who acted on which
token without exposing credentials or query text.

## Decisions

- Add the feature to the existing **Settings → API tokens** experience. Do not
  create a separate Agents section.
- An optional principal can be assigned when creating a token or bound once to
  an existing unbound token. An assigned principal is immutable and displayed
  read-only; changing it still requires revoking the token and creating a new
  one.
- Show the principal as an identity value, not proof that private-memory access
  is active. Private access remains conditional on the server's private-memory
  setting and the token's existing folder ACLs.
- Record both the actor and target on token create/update/revoke audit events.
  Actor attribution uses authenticated token identity (principal and token
  name when available), `Web UI` for cookie-backed sessions, and `CLI` for CLI
  operations. This is source attribution, not a claim about a human user.
- Use the existing authenticated `/api/v1/activity` history for Settings
  activity. Display token lifecycle and write-mutation summaries; omit query
  events and never render arbitrary event payloads. Keep the write-approval
  panel unchanged.

## Scope and behavior

### Token management

- Add the optional `agent_principal` field to the existing active-token list
  response. The response addition is nullable/optional; old clients may ignore
  it.
- Add an optional, labelled principal field to token creation and editing.
  Creation may bind a new token. Editing may bind only an unbound token. A
  bound value is read-only, with an explanation that it cannot be changed or
  cleared in place.
- Rely on the existing backend validation and immutable-binding rule as the
  authority. Surface server validation errors in the existing editor error
  state; do not silently alter a token on failure.
- Token rows show the assigned principal, or indicate no principal is bound.
  Supporting copy explains that private-memory access is additionally gated by
  server configuration and folder ACLs.
- Preserve one-time plaintext token display and never place token values in
  activity events or UI history.

### Actor and target audit details

- Add structured `actor` and `target` values to token lifecycle event payloads.
  The actor is derived server-side from the authenticated request context, not
  from caller-supplied fields. The target contains the affected token name and
  its assigned principal, if any.
- For token API calls, use the authenticated token's name and bound principal
  when available. For cookie-backed UI requests, use `Web UI`; for CLI
  invocations, use `CLI`. Do not persist bearer credentials, UI session IDs,
  or other secrets.
- Create/update/revoke events include the post-operation target principal; for
  revoke, capture it from the token record before revocation. Existing events
  without actor/target fields remain valid and display as legacy events with
  actor unavailable.
- Add the same audit coverage to CLI token create/update/revoke commands so
  lifecycle history is not limited to REST/UI changes.
- The existing activity history remains visible to authenticated feed readers,
  as it is today. No new authorization scope is introduced. The additive
  principal/actor metadata is not treated as a secret.

### Settings activity history

- Replace the write-only "Recent activity" summary with recent safe summaries
  from the existing `/api/v1/activity` history. Keep proposal review and
  approve/reject controls in their existing panel.
- Whitelist event types and display fields for token lifecycle and
  write-mutation events (event label, actor/source, target, safe path/status,
  and timestamp). Do not generically render the event payload.
- Omit `query` events entirely so raw request text is not added to the UI. The
  existing API behavior is otherwise unchanged.
- Preserve accessible loading, error/retry, and empty states. Treat older
  events with absent fields as legacy rather than fabricating actor identity.

## API and data compatibility

- No new endpoint, database column, migration, or change to memory authorization
  is required.
- The token-list `agent_principal` field and event payload fields are
  additive. Existing event envelopes and existing event types remain intact;
  old records continue to render with missing actor/target metadata.
- Use the existing path-sweeping behavior of `/api/v1/activity`; do not weaken
  it or expose arbitrary unswept payload fields in the UI.

## Out of scope

- Automatic conversation capture or new memory writes (Stage 3).
- Changing, clearing, or transferring an assigned principal.
- Making private-memory authorization configurable from the Web UI.
- Showing query strings in the activity UI or changing activity API visibility.
- A new audit API, new database schema, or a broader audit/retention redesign.
- Claiming a named human actor when the system only has a token, CLI source, or
  anonymous/session-based Web UI identity.

## Acceptance checks

1. Token list responses expose `agent_principal`; create and one-time binding
   through the UI send it to the existing endpoints. Bound values are
   read-only, and failed validation leaves tokens unchanged.
2. Token rows and editor copy distinguish an assigned identity from effective
   private access, which still depends on server configuration and folder ACLs.
3. Token create/update/revoke events include target identity and actor source
   for API/UI and CLI operations. Tests prove event payloads never contain
   plaintext tokens or UI session credentials.
4. Actor attribution is server-derived: authenticated token name/principal,
   `Web UI`, or `CLI`; no request field can forge it.
5. Settings loads the existing activity history, renders whitelisted token and
   write-mutation events, omits query events/raw payloads, and preserves loading,
   error/retry, empty, and legacy-event behavior.
6. The write-approval panel and one-time token reveal behavior remain intact;
   existing authenticated history/path-sweeping authorization is unchanged.
7. Focused backend/CLI/API and Web UI tests pass, followed by the full suite.
