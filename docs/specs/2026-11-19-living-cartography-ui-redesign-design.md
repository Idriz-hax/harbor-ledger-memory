# Living cartography UI redesign

**Status:** Design approved; specification awaiting user review

**Date:** 2026-11-19

## Goal

Give Harbor Ledger Memory a distinctive, contemporary visual identity centered
on the living knowledge map. The map should make real agent activity legible as
it travels between notes, while the complete app supports both light and dark
themes.

## Design direction

Use **Living cartography**: a crafted, topographic field-map aesthetic with
layered terrain-like surfaces, fine route lines, expressive but readable
typography, and a multicolour palette with semantic action colors. Avoid both
the current heavy-glow nautical styling and a generic flat admin-dashboard
look. The map is the primary visual feature; controls and status information
remain calm and easy to scan.

Provide light and dark variants of the same design system. On first visit,
follow the operating system color preference; remember an explicit user choice
in browser-local storage. A clearly labelled, accessible theme control remains
available in the app shell. Both themes must preserve the same semantic colors
and legibility.

## Map behavior

- Preserve the current graph's pan, zoom, node selection, drag-to-reposition,
  rearrange, and saved-position behavior. Dragging changes the graph layout,
  not vault content.
- Use the existing live traversal stream as the source of truth for movement.
  Animate a small action signal along a resolved source-to-target edge and
  briefly emphasize the destination. Distinguish read and write actions, and
  use the event mode/type already supplied by the stream; do not invent
  unreported actions or paths.
- Keep activity transient and bounded. Do not leave permanent trails or build
  an independent activity-history layer on the graph.
- Keep existing selection behavior: selecting a node emphasizes its immediate
  neighborhood and shows the selected note context. Keep existing zoom,
  rearrange, and scope controls discoverable in the redesigned map chrome.
- Respect `prefers-reduced-motion`: remove travel/pulse animation while still
  displaying a static action indicator and destination emphasis.

## App-wide visual system

- Redesign the shell, navigation, headings, cards, graph controls, status
  indicators, and Settings/Approvals surfaces to match Living cartography.
- Use multicolour intentionally: reserve distinct hues for action types and
  existing graph categories, with a visible legend and non-colour cues where
  needed. Validate contrast in both themes.
- Retain all existing functionality and copy meaning, including token
  management, access controls, write approvals, and safe activity summaries.
  This is a visual and interaction-polish change, not a permissions or API
  redesign.
- Preserve responsive navigation and layouts, keyboard operation, visible
  focus, semantic labeling, and screen-reader status announcements.

## State coverage

- **Initial load:** keep the app's existing loading and mode-error/retry states,
  styled consistently with the new shell.
- **Graph loading and errors:** retain current loading, offline/traversal status,
  error, retry/rescan, and empty/no-results states. A disconnected live feed
  must be visibly distinct from an idle graph.
- **Live activity:** show movement only for resolved events; do not fabricate a
  route when an endpoint or edge is missing. Keep the map usable when the live
  stream is disconnected.
- **Reduced motion:** replace animated travel with a static, perceivable signal.
- **Settings and approvals:** preserve existing loading, error, empty, and
  confirmation states while applying the new visual system.
- **Theme choice:** render fully in both themes, use system preference until a
  user chooses otherwise, and retain the explicit choice across reloads.

## Scope and boundaries

- Primary implementation surface: `web/src/main.tsx`, `web/src/TideAtlas.tsx`,
  `web/src/appTheme.ts`, and `web/src/styles.css`, with focused Web UI tests.
- Reuse Cytoscape, MUI, the existing graph traversal stream, and current
  position persistence. No new dependency, endpoint, database field, or
  backend authorization change is needed.
- Do not add persistent activity trails, map editing of vault notes, user
  accounts, new graph data, or a new dashboard subsystem.

## Verification

- Add/update focused tests for theme preference and persistence, theme token
  contrast/state behavior, and traversal visualization for valid and unresolved
  events, including reduced motion.
- Run the Web UI test suite and production build.
- Inspect the app at desktop and mobile widths in light and dark themes; verify
  graph controls, keyboard/focus use, and reduced-motion presentation.

## Acceptance checks

1. The app has a cohesive Living cartography redesign, rather than only a restyled
   graph canvas; Settings and Approvals remain usable and consistent.
2. Light and dark themes both render correctly, follow system preference by
   default, and preserve an explicit local choice across reloads.
3. Real traversal events visibly move along their resolved graph edge and mark
   the destination, with action types distinguishable in both themes.
4. Unresolved events do not fabricate map movement; disconnects and idle state
   are distinct and the graph remains usable.
5. Pan/zoom, node dragging, position persistence, selection, and rearrange remain
   available; repositioning never modifies vault content.
6. Reduced-motion users receive static action feedback; responsive, keyboard,
   focus, and screen-reader support remain intact.
7. Focused UI tests and the production UI build pass.
