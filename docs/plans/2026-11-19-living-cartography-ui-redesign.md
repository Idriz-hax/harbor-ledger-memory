# Living Cartography UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign Harbor Ledger Memory around Living cartography with light/dark themes and legible live traversal signals.

**Architecture:** Keep the existing React/MUI shell, Cytoscape graph, traversal stream, and drag/position model. Add a local theme preference, provide light/dark MUI and graph styling, and render a short-lived action marker on resolved live edges. Restyle shared app surfaces without changing backend APIs, authorization, or vault data.

**Tech Stack:** React 19, TypeScript, MUI 7, Cytoscape, CSS, Vitest, Testing Library, Vite.

---

## File map

- `web/src/appTheme.ts`: light/dark palettes, accessible semantic action colors, and initial theme preference resolution.
- `web/src/main.tsx`: theme state, explicit preference persistence, app-shell theme control, MUI theme application.
- `web/src/TideAtlas.tsx`: graph theme input, cartographic graph styling, and consistent controls/legend.
- `web/src/liveTraversal.ts`: short-lived traveling action marker lifecycle for resolved traversal events.
- `web/src/styles.css`: responsive field-map/app surfaces and theme-aware shared surfaces, preserving reduced-motion rules.
- `web/src/__tests__/appTheme.test.ts` (new): mode resolution and palette tests.
- `web/src/__tests__/TideAtlas.test.tsx`: map styling, theme change, and graph interaction coverage.
- `web/src/__tests__/liveTraversal.test.ts`: marker movement, cleanup, unresolved-edge behavior, and reduced-motion coverage.

## Tasks

### Task 1: Theme preference and palette foundation

**Files:** Modify `web/src/appTheme.ts`; create `web/src/__tests__/appTheme.test.ts`.

- [ ] Add failing tests for valid saved `light`/`dark` choice taking precedence over OS preference, invalid/missing value following system preference, and both modes exposing readable palette/action tokens.
- [ ] Run `cd web && npm test -- --run src/__tests__/appTheme.test.ts`; expect failures because mode resolution and light palette are absent.
- [ ] Add a `ColorMode` type and small resolver. Keep preference precedence explicit:

```ts
export type ColorMode = 'light' | 'dark'
export const COLOR_MODE_KEY = 'hlm-color-mode'
export const resolveColorMode = (stored: string | null, prefersDark: boolean): ColorMode =>
  stored === 'light' || stored === 'dark' ? stored : prefersDark ? 'dark' : 'light'
```

- [ ] Extend `makeAppTheme` with a `ColorMode` argument and provide mode-specific surfaces/text while preserving semantic read/write/scan/propose colors with verified contrast.
- [ ] Rerun the focused test and commit `feat: add light and dark app themes`.

### Task 2: Theme control and persistence in the app shell

**Files:** Modify `web/src/main.tsx`; create `web/src/__tests__/appTheme.test.tsx`.

- [ ] Write App-level tests: when local storage has no explicit mode, the shell uses mocked OS preference; clicking the labelled theme control switches mode and persists the explicit value; rerender/reload with that value keeps the selection.
- [ ] Run `cd web && npm test -- --run src/__tests__/appTheme.test.tsx`; expect failure because the shell has no theme preference control.
- [ ] Resolve the initial mode using localStorage and `matchMedia('(prefers-color-scheme: dark)')`, and pass it to `makeAppTheme`. Guard unavailable storage/matchMedia without blocking app startup.
- [ ] Add a labelled MUI `Switch` in the app shell with the accessible name `Dark mode`. Persist only when the user explicitly activates it; do not persist the system-derived default. Apply the explicit mode to document theme state without a reload.

```tsx
const [mode, setMode] = useState(() => {
  let stored: string | null = null
  try { stored = window.localStorage.getItem(COLOR_MODE_KEY) } catch { /* storage may be unavailable */ }
  const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches === true
  return resolveColorMode(stored, prefersDark)
})
const changeMode = (next: ColorMode) => {
  setMode(next)
  try { window.localStorage.setItem(COLOR_MODE_KEY, next) } catch { /* keep the in-memory choice */ }
}
useEffect(() => { document.documentElement.dataset.colorMode = mode }, [mode])
<ThemeProvider theme={makeAppTheme(preset, mode)}>
  <Switch checked={mode === 'dark'} onChange={event => changeMode(event.target.checked ? 'dark' : 'light')} />
</ThemeProvider>
```

- [ ] Keep existing loading, session, navigation, and token behavior unchanged. Run `cd web && npm test -- --run src/__tests__/appTheme.test.tsx src/__tests__/token-auth.test.tsx` and commit `feat: add persistent theme control`.

### Task 3: Cartographic graph in both themes

**Files:** Modify `web/src/TideAtlas.tsx`, `web/src/styles.css`, and `web/src/__tests__/TideAtlas.test.tsx`.

- [ ] Add tests that both modes select their Cytoscape palette, semantic action types retain distinct colors, the legend is visible/labelled, and theme changes preserve viewport and dragged node positions.
- [ ] Run `cd web && npm test -- --run src/__tests__/TideAtlas.test.tsx`; confirm new assertions fail before styling changes.
- [ ] Pass `ColorMode` to `TideAtlas`; derive Cytoscape node, edge, label, selection, activity, and control colors from the mode. Apply a mode change to the existing Cytoscape instance without rebuilding it or resetting viewport/drag positions. Replace deep-sea glow with field-map surfaces, fine route lines, and distinct note groups while keeping colors contrast-safe.
- [ ] Add one visible legend for existing graph categories and read/write/scan/propose signals; retain text cues so color is not the only distinction. Reuse current pan/zoom, rearrange, scope, and selection controls.

```css
:root[data-color-mode='light'] { --canvas: #f0eee6; --surface: #faf9f3; --ink: #26352f; --route: #739982; }
:root[data-color-mode='dark'] { --canvas: #101b1e; --surface: #142326; --ink: #e5efe9; --route: #618b79; }
```

- [ ] Make shell and map CSS responsive in both modes; retain focus visibility and `prefers-reduced-motion`. Run focused TideAtlas tests and commit `feat: restyle graph as living cartography`.

### Task 4: Moving traversal signals

**Files:** Modify `web/src/liveTraversal.ts`, `web/src/TideAtlas.tsx`, `web/src/styles.css`; test `web/src/__tests__/liveTraversal.test.ts` and `web/src/__tests__/TideAtlas.test.tsx`.

- [ ] Add failing controller tests for a short-lived marker traveling from a resolved source to target, read/write color distinction, cleanup after completion/dispose, no marker for unresolved endpoints/edges, and a static signal when reduced motion is enabled.
- [ ] Run `cd web && npm test -- --run src/__tests__/liveTraversal.test.ts`; verify the new marker assertions fail.
- [ ] For an event with a resolved edge, resolve source/target IDs from that edge's `data('source')` and `data('target')`; if any part is missing, do not create a marker. Add a uniquely identified temporary Cytoscape marker at the source and animate it to the target; keep the existing edge and destination emphasis. Track/remove the marker on timeout, cancellation, and controller disposal; never add it to fetched graph data or position persistence.
- [ ] Style read/write markers distinctly in both themes and add the reduced-motion static edge signal without invoking animations. Run live traversal and TideAtlas tests; commit `feat: animate live graph traversals`.

```ts
const marker = core.add({ group: 'nodes', data: { id: markerId }, position: source.position(), classes: `traversal-marker traversal-${event.mode}` })
marker.animate({ position: target.position(), duration: options.duration ?? 900 })
// Remove the temporary element on completion, cancellation, or dispose.
```

### Task 5: Apply Living cartography to the rest of the app

**Files:** Modify `web/src/main.tsx`, `web/src/appTheme.ts`, `web/src/styles.css`; test `web/src/__tests__/settings-writes.test.tsx`, `web/src/__tests__/token-auth.test.tsx`, and `web/src/__tests__/appTheme.test.tsx`.

- [ ] Add focused assertions that Settings, approvals, token editor, alerts, navigation, and responsive shell remain present and legible in both themes; preserve existing behavior assertions.
- [ ] Restyle sidebar/navigation and page hierarchy first; keep graph controls visually prominent but distinct from navigation.
- [ ] Restyle Settings/Approvals panels, buttons, dialogs, token rows, and activity rows with the same surface, border, and spacing tokens. Keep token plaintext reveal behavior unchanged.
- [ ] Apply the same semantic tokens to loading, empty, success, error, and disconnected states; ensure each is distinguishable without color alone.
- [ ] Keep approval and token controls semantically unchanged; preserve their loading, error, confirmation, and one-time-secret states. Do not add routes, settings, or backend behavior.
- [ ] Run `cd web && npm test -- --run src/__tests__/settings-writes.test.tsx src/__tests__/token-auth.test.tsx src/__tests__/appTheme.test.tsx`; commit `feat: apply cartographic theme across app`.

### Task 6: Final regression and visual verification

**Files:** All files changed above; add only regression fixes required by verification.

- [ ] Run focused frontend tests: `cd web && npm test -- --run src/__tests__/appTheme.test.ts src/__tests__/appTheme.test.tsx src/__tests__/TideAtlas.test.tsx src/__tests__/liveTraversal.test.ts src/__tests__/settings-writes.test.tsx src/__tests__/token-auth.test.tsx`.
- [ ] Run full frontend tests: `cd web && npm test -- --run`; expect all tests to pass.
- [ ] Run production checks: `cd web && npm run build`; expect TypeScript and Vite build to succeed.
- [ ] Inspect desktop and mobile layouts in light and dark modes. Verify graph movement, selection, drag/persist/rearrange, live feed disconnect/reconnect, reduced motion, keyboard focus, theme toggle labeling, and no horizontal overflow.
- [ ] Run `git diff --check`; review the final diff against `docs/specs/2026-11-19-living-cartography-ui-redesign-design.md`. Commit any focused verification repair separately; report any untested browser/a11y checks honestly.
