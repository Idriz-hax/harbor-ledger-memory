# Settings, approvals, and session renewal design

## Purpose

Separate configuration from operational review work, while allowing an open UI to remain authenticated without a manual browser refresh after idle time.

## Scope

- Add a dedicated **Approvals** destination to the primary navigation.
- Move pending write proposals and their approve/reject controls from Settings to Approvals.
- Keep Settings focused on vault configuration, access/token controls, and the completed write audit.
- Renew an authenticated browser session while the UI tab remains open.
- When renewal cannot succeed, present sign-in and preserve the original destination for return after authentication.

## Navigation and layout

- Primary navigation contains `Memory Chart`, `Approvals`, and `Settings`.
- Approvals has a numeric badge only when proposals are pending.
- The Approvals screen is the sole interactive review queue. It retains the current utilitarian treatment and response-authoritative updates.
- Settings removes pending proposal controls. Its audit history remains available as a read-only record of applied, rejected, and failed proposals.
- No broader visual-system redesign is in scope; hierarchy and grouping are refined within the existing styles.

## Approvals states

- **Loading:** preserve an existing queue while refreshing; show a small refresh indicator. On first load, show a stable skeleton.
- **Pending:** show each proposal with clear approve and reject controls. Disable only the proposal being actioned and show its busy state.
- **Empty:** show `All caught up. Nothing needs review.`
- **Error:** retain successfully loaded items when possible and provide a retry action.
- **Session expired:** route to sign-in rather than leaving action controls that fail silently.

## Session behavior

- The existing 30-minute idle timeout and 24-hour absolute timeout remain server-side security boundaries.
- After browser authentication, the frontend sends a lightweight authenticated keepalive before the 30-minute idle deadline while the document is visible. This renews the existing cookie session through the server's normal session lookup rather than extending it indefinitely.
- The keepalive stops when the tab is hidden and resumes when it is visible, immediately validating the session on return.
- The shared API client treats `401 Unauthorized` as an expired browser session: it stores the current in-app destination, starts the sign-in flow, and avoids presenting generic request errors.
- Sign-in returns the user to their prior destination, including Approvals. A 403 remains an authorization error and is not treated as session expiry.
- Event-stream failures caused by an expired session follow the same sign-in transition and do not require a browser refresh.

## Boundaries

- Browser UI sessions remain cookie-only; CSRF requirements for unsafe requests are unchanged.
- Token-based REST/MCP clients do not use browser keepalive or sign-in redirects.
- No new dependencies, session-storage protocol, or API version change is required.

## Verification

- Backend tests prove an authenticated keepalive refreshes idle activity but never bypasses absolute expiry.
- Frontend tests prove the primary navigation, pending badge, review actions, empty/error states, 401 redirect, preserved return destination, and 403 behavior.
- Existing session, CSRF, settings, and token-management tests remain green.
