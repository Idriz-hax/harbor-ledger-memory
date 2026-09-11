import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import cytoscape, { Core, ElementDefinition } from 'cytoscape'
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  CssBaseline,
  Divider,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Paper,
  Select,
  TextField,
  ThemeProvider,
  Typography,
  ToggleButton,
  ToggleButtonGroup,
} from '@mui/material'
import './styles.css'
import { DEFAULT_PRESET, makeAppTheme, presetFor, type ChartPreset } from './appTheme'
import { TideAtlas } from './TideAtlas'
import '@fontsource/newsreader/400.css'
import '@fontsource/newsreader/600.css'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'

type Screen = 'graph' | 'settings'
const SCREEN_META: Record<Screen, { label: string; icon: string; description: string }> = {
  graph: { label: 'Memory Chart', icon: '⌁', description: 'Explore linked notes and live memory routes' },
  settings: { label: 'Vault Permissions', icon: '⌑', description: 'Control what agents can read and write' },
}
export type Activity = { id: number; event_type: string; created_at: string; payload: Record<string, unknown> }
type Status = { indexed_notes: number; scan_runs: number; diagnostics: number; broken_links: number; ambiguous_links: number; last_scan_status: string | null; last_scan_completed_at: string | null; effective_read_scope: string }

export class APIError extends Error {
  readonly status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}
/* Prefer the backend's `detail` field when the error body is JSON, so the UI
   never shows a raw payload string to the user. */
const errorDetail = (text: string) => {
  try { const parsed = JSON.parse(text) as { detail?: unknown }; if (parsed && typeof parsed.detail === 'string') return parsed.detail } catch { /* plain-text body */ }
  return text
}
export const readCookie = (name: string): string | null => {
  if (typeof document === 'undefined') return null
  const prefix = `${encodeURIComponent(name)}=`
  const entry = document.cookie.split(';').map(cookie => cookie.trim()).find(cookie => cookie.startsWith(prefix))
  if (!entry) return null
  const value = entry.slice(prefix.length)
  try { return decodeURIComponent(value) } catch { return value }
}
export const api = async <T,>(url: string, init?: RequestInit) => {
  const method = (init?.method ?? 'GET').toUpperCase()
  const sameOrigin = typeof window !== 'undefined' && new URL(url, window.location.href).origin === window.location.origin
  const csrf = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && sameOrigin ? readCookie('hlm_ui_csrf') : null
  const requestInit = csrf ? {
    ...init,
    headers: init?.headers instanceof Headers
      ? new Headers([...init.headers, ['X-HLM-CSRF', csrf]])
      : Array.isArray(init?.headers)
        ? [...init.headers, ['X-HLM-CSRF', csrf] as [string, string]]
        : { ...(init?.headers as Record<string, string> | undefined), 'X-HLM-CSRF': csrf },
  } : init
  const response = await fetch(url, requestInit)
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new APIError(response.status, errorDetail(text) || response.statusText)
  }
  return response.json() as Promise<T>
}
export type TokenRule = { path: string; access: string }
export type TokenSummary = { name: string; rules: TokenRule[]; admin: boolean; approve_own_proposals: boolean; created_at: string }
/* Short, scannable rule summary for a token list row: only the rules that
   deviate from the default read are named. */
const RULE_SUMMARY_LABEL: Record<string, string> = { none: 'none', 'propose-write': 'propose', 'auto-write': 'write' }
export const ruleSummary = (rules: TokenRule[] | null | undefined): string => {
  const special = (rules ?? []).filter(rule => rule && rule.access && rule.access !== 'read')
  if (special.length === 0) return 'read-only'
  return special.map(rule => `${rule.path === '.' ? 'vault' : rule.path}: ${RULE_SUMMARY_LABEL[rule.access] ?? rule.access}`).join(' · ')
}
const tokenNameSuggestion = () => `agent-${new Date().toISOString().slice(0, 10)}`
const formatCreated = (stamp: string) => {
  const date = new Date(stamp)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}
const timeAgo = (stamp: string) => { const seconds = Math.max(0, (Date.now() - new Date(stamp).getTime()) / 1000); if (seconds < 60) return `${Math.round(seconds)}s ago`; if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`; return `${Math.round(seconds / 3600)}h ago` }
const label = (value: string) => value.replaceAll('_', ' ')
const prefersReducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

/* --- App shell --- */
export function App() {
  const [screen, setScreen] = useState<Screen>('graph')
  const [events, setEvents] = useState<Activity[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  /* The calm-neutral chart preset follows the vault's saved theme so the whole
     shell (not just the canvas) reflects the operator's choice. */
  const [preset, setPreset] = useState<ChartPreset>(DEFAULT_PRESET)

  const refresh = useCallback(() => {
    api<Status>('/api/v1/status')
      .then(() => setError(''))
      .catch(e => setError(e.message))
    api<{ events: Activity[] }>('/api/v1/activity?limit=40').then(r => setEvents(r.events)).catch(() => undefined)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    api<{ theme?: { preset?: string } }>('/api/v1/settings')
      .then(r => setPreset(presetFor(r?.theme?.preset)))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    const stream = new EventSource('/api/v1/activity/stream')
    stream.onopen = () => setConnected(true)
    stream.onerror = () => setConnected(false)
    stream.addEventListener('activity', e => {
      const next = JSON.parse((e as MessageEvent).data) as Activity
      setEvents(old => [...old.filter(item => item.id !== next.id), next].slice(-80))
    })
    return () => stream.close()
  }, [])

  return (
    <ThemeProvider theme={makeAppTheme(preset)}>
      <CssBaseline />
      <Box sx={{ display: 'flex', minHeight: '100vh', background: 'transparent' }}>
        <Box component="aside" sx={{ width: 252, flexShrink: 0, p: 3, borderRight: 1, borderColor: 'divider', bgcolor: 'background.paper', display: 'flex', flexDirection: 'column', gap: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, pb: 3, mb: 2, borderBottom: 1, borderColor: 'divider' }}>
            <Box component="span" aria-hidden sx={{ display: 'grid', width: 38, height: 38, placeItems: 'center', borderRadius: '50%', border: 1, borderColor: 'secondary.main', color: 'secondary.main', fontSize: 17 }}>✦</Box>
            <Box>
              <Typography sx={{ fontWeight: 700, fontSize: 13, letterSpacing: '.12em' }}>HARBOR LEDGER</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ fontFamily: '"IBM Plex Mono", monospace', letterSpacing: '.12em' }}>MEMORY SERVICE</Typography>
            </Box>
          </Box>
          <nav aria-label="Primary navigation">
            <List disablePadding>
              {(['graph', 'settings'] as Screen[]).map((item, index) => (
                <ListItemButton
                  key={item}
                  selected={screen === item}
                  onClick={() => setScreen(item)}
                  aria-label={`0${index + 1} ${item} — ${SCREEN_META[item].description}`}
                  aria-current={screen === item ? 'page' : undefined}
                  title={SCREEN_META[item].label}
                  sx={{ minHeight: 52, mb: 1, gap: 1.5 }}
                >
                  <ListItemText primary={SCREEN_META[item].label} primaryTypographyProps={{ variant: 'subtitle2', fontWeight: 600 }} />
                </ListItemButton>
              ))}
            </List>
          </nav>
          <Box sx={{ mt: 'auto', pt: 2, borderTop: 1, borderColor: 'divider', display: 'flex', alignItems: 'center', gap: 1, color: 'text.secondary', fontSize: 12 }}>
            <Box component="span" aria-hidden sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: connected ? 'success.main' : 'error.main', display: 'inline-block', flexShrink: 0 }} />
            <span>{connected ? 'activity connected' : 'reconnecting'}</span>
          </Box>
        </Box>
        <Box component="main" sx={{ flexGrow: 1, width: '100%', minWidth: 0, p: { xs: 2, sm: 3, md: 4 }, display: 'flex', flexDirection: 'column', gap: 2, maxWidth: 'none' }}>
          <Box component="header" sx={{ mb: 1 }}>
            <Typography variant="overline" color="text.secondary" sx={{ display: 'block', mb: 1 }}>LOCAL · PRIVATE · AUDITABLE MEMORY</Typography>
            <Typography variant="h2">{SCREEN_META[screen].label}</Typography>
          </Box>
          {error && <Alert severity="error">{error}</Alert>}
          {screen === 'graph' && <TideAtlas events={events} onRefresh={refresh} />}
          {screen === 'settings' && <Settings />}
        </Box>
      </Box>
    </ThemeProvider>
  )
}

/* --- Graph screen --- */

type GraphNodeData = { path: string; title: string; isolated: boolean }
type GraphEdgeData = { id: string; source: string; target: string; edge_type: string; explicit: boolean }
type GraphSnapshot = { nodes: GraphNodeData[]; edges: GraphEdgeData[]; generation: string | number }
type PathSegment = { source: string; target: string; edge_type?: string; traversal_direction?: 'forward' | 'backward' }
type GraphPhase = 'loading' | 'ready' | 'error'
type AnyCollection = ReturnType<Core['elements']>

/* Live trails advance one real graph edge at a time, then briefly mark the
   destination before returning every element to its ordinary category colour. */
export const PULSE_MS = 260
export const TRAIL_HOLD_MS = 2500

/* Activity event type → cytoscape node class (approved dark palette). */
const ACTIVITY_NODE_CLASS: Record<string, string> = {
  query: 'query-active',
  scan: 'scan-active',
  'vault.mutation.requested': 'proposal-active',
  config: 'proposal-active',
  'vault.mutation.applied': 'write-active',
}
/* Events that change the vault topology — refetch the graph snapshot. */
const REFRESH_EVENT_TYPES = new Set(['scan', 'vault.mutation.applied'])
const NODE_ACTIVITY_CLASSES = [...new Set(Object.values(ACTIVITY_NODE_CLASS))].join(' ')
/* A compact, colour-blind-friendly family: folders differ clearly, while live
   activity keeps the brighter semantic colours (read/index/propose/write). */
const FOLDER_PALETTE = ['#38d6c0', '#2fa99f', '#75cfc1', '#f2b84b', '#d99a36', '#9ce8dc']
const FLOW_FOLDER_CLASSES = FOLDER_PALETTE.map((_, index) => `flow-folder-${index}`).join(' ')
const TRANSIT_NODE_CLASSES = FOLDER_PALETTE.map((_, index) => `transit-folder-${index}`).join(' ')
const FLOW_EDGE_CLASSES = `path-active flow-query flow-scan flow-proposal flow-write flow-reverse ${FLOW_FOLDER_CLASSES}`
const EDGE_VISUAL_PRIORITY: Record<string, number> = { links_to: 3, parent_of: 2, contains: 1 }
/* New placement engine: do not reuse coordinates saved by earlier layouts. */
const GRAPH_LAYOUT_KEY = 'neural-memory.graph-layout.v12'
type GraphLayoutState = { positions: Record<string, { x: number; y: number }>; pinned: string[] }

const readGraphLayout = (): GraphLayoutState => {
  try {
    const parsed = JSON.parse(localStorage.getItem(GRAPH_LAYOUT_KEY) || '{}') as Partial<GraphLayoutState>
    return { positions: parsed.positions ?? {}, pinned: parsed.pinned ?? [] }
  } catch { return { positions: {}, pinned: [] } }
}
const writeGraphLayout = (layout: GraphLayoutState) => {
  try { localStorage.setItem(GRAPH_LAYOUT_KEY, JSON.stringify(layout)) } catch { /* Storage is optional. */ }
}

const stringList = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [])
const pathSegments = (value: unknown): PathSegment[] =>
  Array.isArray(value) ? value.filter((s): s is PathSegment => !!s && typeof s === 'object' && typeof (s as PathSegment).source === 'string' && typeof (s as PathSegment).target === 'string') : []
type Position = { x: number; y: number }
type GroupGas = { id: string; color: string; path: string }
const side = (a: Position, b: Position, c: Position) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
const segmentsIntersect = (a: Position, b: Position, c: Position, d: Position) =>
  Math.sign(side(a, b, c)) !== Math.sign(side(a, b, d)) && Math.sign(side(c, d, a)) !== Math.sign(side(c, d, b))
const pointSegmentDistance = (point: Position, start: Position, end: Position) => {
  const dx = end.x - start.x, dy = end.y - start.y
  const lengthSquared = dx * dx + dy * dy || 1
  const ratio = Math.max(0, Math.min(1, ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared))
  return Math.hypot(point.x - (start.x + dx * ratio), point.y - (start.y + dy * ratio))
}
const convexHull = (points: Position[]) => {
  if (points.length < 3) return points
  const sorted = [...points].sort((a, b) => a.x - b.x || a.y - b.y)
  const build = (items: Position[]) => items.reduce<Position[]>((hull, point) => {
    while (hull.length > 1 && side(hull[hull.length - 2], hull[hull.length - 1], point) <= 0) hull.pop()
    hull.push(point)
    return hull
  }, [])
  const lower = build(sorted)
  const upper = build([...sorted].reverse())
  return [...lower.slice(0, -1), ...upper.slice(0, -1)]
}
const gasPath = (points: Position[]) => {
  if (!points.length) return ''
  const center = points.reduce((sum, point) => ({ x: sum.x + point.x / points.length, y: sum.y + point.y / points.length }), { x: 0, y: 0 })
  const hull = convexHull(points)
  const padded = (hull.length === 1 ? [0, 1, 2, 3].map(index => ({ x: center.x + Math.cos(index * Math.PI / 2) * 74, y: center.y + Math.sin(index * Math.PI / 2) * 74 })) : hull)
    .map(point => {
      const dx = point.x - center.x, dy = point.y - center.y
      const distance = Math.hypot(dx, dy) || 1
      const padding = 38
      return { x: point.x + dx / distance * padding, y: point.y + dy / distance * padding }
    })
  if (padded.length === 2) {
    const [a, b] = padded
    const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1
    const px = -dy / length * 62, py = dx / length * 62
    return `M ${a.x + px} ${a.y + py} Q ${center.x + px * 1.7} ${center.y + py * 1.7} ${b.x + px} ${b.y + py} Q ${center.x - px * 1.7} ${center.y - py * 1.7} ${a.x - px} ${a.y - py} Z`
  }
  const start = padded[0]
  let path = `M ${start.x} ${start.y}`
  padded.forEach((point, index) => {
    const next = padded[(index + 1) % padded.length]
    path += ` Q ${point.x} ${point.y} ${(point.x + next.x) / 2} ${(point.y + next.y) / 2}`
  })
  return `${path} Z`
}

export function Graph({ events, onRefresh }: { events: Activity[]; onRefresh?: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const graphCardRef = useRef<HTMLDivElement>(null)
  const cy = useRef<Core | null>(null)
  const [scanning, setScanning] = useState(false)
  const [rescanError, setRescanError] = useState('')
  const [snapshot, setSnapshot] = useState<GraphSnapshot | null>(null)
  const [phase, setPhase] = useState<GraphPhase>('loading')
  const [graphError, setGraphError] = useState('')
  const [staleError, setStaleError] = useState('')
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [folderScope, setFolderScope] = useState('all')
  const [showDisconnected, setShowDisconnected] = useState(true)
  const [pinnedPaths, setPinnedPaths] = useState<Set<string>>(() => new Set(readGraphLayout().pinned))
  const [layoutNotice, setLayoutNotice] = useState('')
  const [layoutEpoch, setLayoutEpoch] = useState(0)
  const [groupGas, setGroupGas] = useState<GroupGas[]>([])
  const [auraViewport, setAuraViewport] = useState({ width: 1, height: 1 })
  const [isGraphFullscreen, setIsGraphFullscreen] = useState(false)
  const lastEventId = useRef<number | null>(null)
  /* Flow events that arrived before the cytoscape core existed. */
  const pendingEvents = useRef<Activity[]>([])
  /* Most recent flow event — replayed after rebuilds so refreshed nodes stay highlighted. */
  const lastFlowEvent = useRef<Activity | null>(null)
  const pulse = useRef<{ cancelled: boolean; nodeEles: AnyCollection | null; edgeEles: AnyCollection | null; cleanupTimer: number | null } | null>(null)
  const alive = useRef(true)
  const unmounting = useRef(false)
  /* Monotonic fetch sequence: only the latest graph fetch may commit state,
     so out-of-order responses can never clobber a newer snapshot. */
  const loadSeq = useRef(0)
  /* Whether a snapshot has been committed during this mount. A refresh that fails
     before the first commit must surface as the visible initial-error state — a
     stale flag alone would hide behind phase='loading' with no snapshot to show. */
  const committed = useRef(false)

  useEffect(() => {
    alive.current = true
    unmounting.current = false
    return () => {
      unmounting.current = true
      alive.current = false
      const current = pulse.current
      if (current) {
        current.cancelled = true
        current.nodeEles?.stop()
        current.edgeEles?.stop()
        if (current.cleanupTimer !== null) window.clearTimeout(current.cleanupTimer)
      }
    }
  }, [])

  /* --- Snapshot loading (GET /api/v1/graph): initial loads model loading/error,
      background refresh failures leave the last snapshot visible as stale — unless
      no snapshot was ever committed, in which case a failed refresh is an initial
      error (visible + retryable) and not a hidden stale flag. --- */
  const loadGraph = useCallback((kind: 'initial' | 'refresh') => {
    const seq = ++loadSeq.current
    if (kind === 'initial') setPhase('loading')
    api<GraphSnapshot>('/api/v1/graph')
      .then(s => {
        if (!alive.current || seq !== loadSeq.current) return
        setSnapshot(s)
        committed.current = true
        setGraphError('')
        setStaleError('')
        setPhase('ready')
      })
      .catch(e => {
        if (!alive.current || seq !== loadSeq.current) return
        /* A refresh failure is only "stale data" when a snapshot is on screen;
           otherwise it is an initial failure and must be visible + retryable. */
        if (kind === 'refresh' && committed.current) setStaleError(e.message)
        else { setPhase('error'); setGraphError(e.message); setStaleError('') }
      })
  }, [])

  useEffect(() => { loadGraph('initial') }, [loadGraph])

  /* --- Live activity playback: traverse the real route in order, leave a
      brief trail, then illuminate only the terminal note. --- */
  const startPulse = useCallback((core: Core, route: Array<{ edge: AnyCollection; className: string; reverse: boolean; departure: AnyCollection; arrival: AnyCollection; transitClass: string }>, terminal: AnyCollection | null, nodeClass?: string) => {
    const prev = pulse.current
    if (prev) {
      prev.cancelled = true
      prev.nodeEles?.stop()
      prev.edgeEles?.stop()
      if (prev.cleanupTimer !== null) window.clearTimeout(prev.cleanupTimer)
    }
    const handle = { cancelled: false, nodeEles: null as AnyCollection | null, edgeEles: null as AnyCollection | null, cleanupTimer: null as number | null }
    pulse.current = handle
    const motionDuration = prefersReducedMotion() ? 0 : PULSE_MS
    const finish = () => {
      if (handle.cancelled) return
      if (terminal?.length && nodeClass) {
        terminal.addClass(nodeClass)
         terminal.animate({ style: { width: 25 }, duration: motionDuration, easing: 'ease-out' })
        handle.nodeEles = terminal
      }
      handle.edgeEles = core.edges().filter(edge => edge.hasClass('path-active'))
      handle.cleanupTimer = window.setTimeout(() => {
        if (handle.cancelled) return
        core.batch(() => {
          handle.nodeEles?.removeClass(NODE_ACTIVITY_CLASSES)
          handle.edgeEles?.removeClass(FLOW_EDGE_CLASSES)
          core.nodes().removeClass(TRANSIT_NODE_CLASSES)
        })
        if (pulse.current === handle) pulse.current = null
      }, TRAIL_HOLD_MS)
    }
    const step = (index: number) => {
      if (handle.cancelled) return
      const segment = route[index]
      if (!segment) { finish(); return }
      if (index === 0) segment.departure.addClass(segment.transitClass)
      segment.edge.addClass(segment.className)
      handle.edgeEles = core.edges().filter(edge => edge.hasClass('path-active'))
      segment.edge.animate({
        style: { opacity: 1, 'line-dash-offset': segment.reverse ? -18 : 18 },
         duration: motionDuration,
        easing: 'ease-in-out',
        complete: () => { segment.arrival.addClass(segment.transitClass); step(index + 1) },
      })
    }
    step(0)
  }, [])

  /* --- Activity flows: apply graph_refs / graph_path classes on the live core --- */
  const applyActivity = useCallback((item: Activity) => {
    const core = cy.current
    if (!core) {
      /* Core not initialized yet — queue the event for replay at core init. */
      pendingEvents.current = [...pendingEvents.current.filter(e => e.id !== item.id), item].slice(-10)
      return
    }
    const nodeClass = ACTIVITY_NODE_CLASS[item.event_type]
    const nodePaths = stringList(item.payload.graph_refs)
    const segments = pathSegments(item.payload.graph_path)
    const route: Array<{ edge: AnyCollection; className: string; reverse: boolean; departure: AnyCollection; arrival: AnyCollection; transitClass: string }> = []
    core.batch(() => {
      core.nodes().removeClass(NODE_ACTIVITY_CLASSES)
      core.edges().removeClass(FLOW_EDGE_CLASSES)
      if (segments.length) {
        segments.forEach(seg => {
          const path = core.edges().filter(e => e.data('source') === seg.source && e.data('target') === seg.target && (!seg.edge_type || e.data('edge_type') === seg.edge_type))
          const source = core.getElementById(seg.source)
          const folderClass = FOLDER_PALETTE.findIndex((_, index) => source.hasClass(`folder-${index}`))
          const target = core.getElementById(seg.target)
          const reverse = seg.traversal_direction === 'backward'
          route.push({ edge: path, reverse, departure: source, arrival: target, transitClass: `transit-folder-${Math.max(0, folderClass)}`, className: `path-active flow-folder-${Math.max(0, folderClass)} flow-${item.event_type === 'vault.mutation.applied' ? 'write' : item.event_type === 'vault.mutation.requested' || item.event_type === 'config' ? 'proposal' : item.event_type}${reverse ? ' flow-reverse' : ''}` })
        })
      }
    })
    const terminalPath = (segments.length ? segments[segments.length - 1].target : undefined) ?? (nodePaths.length ? nodePaths[nodePaths.length - 1] : undefined)
    startPulse(core, route.filter(segment => segment.edge.length > 0), terminalPath ? core.getElementById(terminalPath) : null, nodeClass)
  }, [startPulse])

  /* React to new activity events: highlight, and refresh after scans/applied writes. */
  useEffect(() => {
    const seen = lastEventId.current
    if (seen === null) {
      /* Baseline: do not reprocess the activity history fetched on mount. */
      lastEventId.current = events.reduce((max, e) => Math.max(max, e.id), 0)
      return
    }
    const fresh = events.filter(e => e.id > seen)
    if (fresh.length === 0) return
    lastEventId.current = Math.max(seen, ...fresh.map(e => e.id))
    fresh.forEach(e => {
      if (!ACTIVITY_NODE_CLASS[e.event_type] && pathSegments(e.payload.graph_path).length === 0) return
      lastFlowEvent.current = e
      applyActivity(e)
    })
    if (fresh.some(e => REFRESH_EVENT_TYPES.has(e.event_type))) loadGraph('refresh')
  }, [events, applyActivity, loadGraph])

  /* --- Build the cytoscape core from the API snapshot (rebuild only per generation) --- */
  const allNodes = snapshot?.nodes ?? []
  const folderOptions = useMemo(() => [...new Set(allNodes.map(node => node.path.split('/')[0]).filter(Boolean))].sort(), [allNodes])
  const scopedNodes = useMemo(() => folderScope === 'all' ? allNodes : allNodes.filter(node => node.path === folderScope || node.path.startsWith(`${folderScope}/`)), [allNodes, folderScope])
  const disconnectedCount = useMemo(() => scopedNodes.filter(node => node.isolated).length, [scopedNodes])
  /* A handful of standalone notes are useful context; a large disconnected
     set becomes visual noise, so only collapse it once it is substantial. */
  const hideDisconnected = !showDisconnected && disconnectedCount >= 8
  const visibleNodes = useMemo(() => hideDisconnected ? scopedNodes.filter(node => !node.isolated) : scopedNodes, [hideDisconnected, scopedNodes])
  const visibleNodePaths = useMemo(() => new Set(visibleNodes.map(node => node.path)), [visibleNodes])
  const visibleEdges = useMemo(() => {
    /* The API retains parallel structural semantics for memory traversal. The
       canvas renders one strongest relationship per pair so it never draws a
       shadow duplicate over the same string. */
    const strongest = new Map<string, GraphEdgeData>()
    ;(snapshot?.edges ?? []).forEach(edge => {
      if (!visibleNodePaths.has(edge.source) || !visibleNodePaths.has(edge.target)) return
      const pair = [edge.source, edge.target].sort().join('\u0000')
      const current = strongest.get(pair)
      if (!current || (EDGE_VISUAL_PRIORITY[edge.edge_type] ?? 0) > (EDGE_VISUAL_PRIORITY[current.edge_type] ?? 0)) strongest.set(pair, edge)
    })
    return [...strongest.values()]
  }, [snapshot, visibleNodePaths])
  const nodeDegree = useMemo(() => {
    const degrees = new Map<string, number>()
    visibleEdges.forEach(edge => {
      degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1)
      degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1)
    })
    return degrees
  }, [visibleEdges])
  const folderIndex = useMemo(() => new Map(folderOptions.map((folder, index) => [folder, index])), [folderOptions])
  const generation = `${snapshot?.generation ?? ''}:${folderScope}:${showDisconnected}:${layoutEpoch}`
  useEffect(() => {
    const container = ref.current
    if (!container || !snapshot || !snapshot.generation) return
    if (visibleNodes.length === 0) return
    const savedLayout = readGraphLayout()
    /* A partial preset creates the artificial baseline visible in the old map:
       only reuse a saved layout when every visible note has a coordinate. */
    const hasCompleteSavedLayout = visibleNodes.every(node => savedLayout.positions[node.path])
    const shouldReuseSavedLayout = folderScope === 'all' && hasCompleteSavedLayout
    /* Every note lands near an already placed note. This gives the map an
       organic, mixed field without folder grids, gravity wells, or far islands. */
    const seedPositions = new Map<string, { x: number; y: number }>()
    const folderPositions = new Map<string, Array<{ x: number; y: number }>>()
    const neighbours = new Map<string, string[]>()
    visibleEdges.forEach(edge => {
      neighbours.set(edge.source, [...(neighbours.get(edge.source) ?? []), edge.target])
      neighbours.set(edge.target, [...(neighbours.get(edge.target) ?? []), edge.source])
    })
    const placementOrder = [...visibleNodes].sort((a, b) => (nodeDegree.get(b.path) ?? 0) - (nodeDegree.get(a.path) ?? 0))
    placementOrder.forEach((node, index) => {
      const folder = node.path.split('/')[0]
      if (index === 0) {
        const origin = { x: 0, y: 0 }
        seedPositions.set(node.path, origin)
        folderPositions.set(folder, [origin])
        return
      }
      const linkedPaths = (neighbours.get(node.path) ?? []).filter(path => seedPositions.has(path))
      const linked = linkedPaths.map(path => seedPositions.get(path)).filter((point): point is Position => !!point)
      const placed = [...seedPositions.values()]
      const sameFolder = folderPositions.get(folder) ?? []
      const anchors = sameFolder.length ? sameFolder : linked.length ? linked : placed
      const anchor = anchors[Math.floor(Math.random() * anchors.length)]
      const existingEdges = visibleEdges.filter(edge => seedPositions.has(edge.source) && seedPositions.has(edge.target))
      let position = anchor, lowestPenalty = Number.POSITIVE_INFINITY
      for (let attempt = 0; attempt < 24; attempt++) {
        const angle = Math.random() * Math.PI * 2
        const distance = 135 + Math.random() * 165
        const candidate = { x: anchor.x + Math.cos(angle) * distance, y: anchor.y + Math.sin(angle) * distance }
        if (!placed.every(point => Math.hypot(point.x - candidate.x, point.y - candidate.y) > 72)) continue
        const crossings = linkedPaths.reduce((total, targetPath) => {
          const target = seedPositions.get(targetPath)!
          return total + existingEdges.filter(edge => edge.source !== targetPath && edge.target !== targetPath && segmentsIntersect(candidate, target, seedPositions.get(edge.source)!, seedPositions.get(edge.target)!)).length
        }, 0)
        const foreignLinkClearance = existingEdges.filter(edge => {
          const sourceFolder = edge.source.split('/')[0], targetFolder = edge.target.split('/')[0]
          return sourceFolder !== folder && targetFolder !== folder && pointSegmentDistance(candidate, seedPositions.get(edge.source)!, seedPositions.get(edge.target)!) < 56
        }).length
        const penalty = crossings * 10 + foreignLinkClearance
        if (penalty < lowestPenalty) { position = candidate; lowestPenalty = penalty }
        if (penalty === 0) break
      }
      seedPositions.set(node.path, position)
      folderPositions.set(folder, [...sameFolder, position])
    })

    /* Settle the initial scatter as a small force field. Links retain an
       easy-to-read length, every star gets personal space, and folder affinity
       only stops a folder from drifting apart — it never packs it into a ball. */
    const edgePairs = visibleEdges.map(edge => ({ source: edge.source, target: edge.target, rest: edge.edge_type === 'links_to' ? 190 : 164 }))
    for (let iteration = 0; iteration < 96; iteration++) {
      const forces = new Map<string, Position>(placementOrder.map(node => [node.path, { x: 0, y: 0 }]))
      for (let left = 0; left < placementOrder.length; left++) {
        const a = placementOrder[left], aPosition = seedPositions.get(a.path)!
        for (let right = left + 1; right < placementOrder.length; right++) {
          const b = placementOrder[right], bPosition = seedPositions.get(b.path)!
          let dx = bPosition.x - aPosition.x, dy = bPosition.y - aPosition.y
          let distance = Math.hypot(dx, dy)
          if (distance > 188) continue
          if (distance < 0.01) { dx = Math.random() - .5; dy = Math.random() - .5; distance = Math.hypot(dx, dy) }
          const strength = Math.pow((188 - distance) / 188, 2) * 8.5
          const aForce = forces.get(a.path)!, bForce = forces.get(b.path)!
          aForce.x -= dx / distance * strength; aForce.y -= dy / distance * strength
          bForce.x += dx / distance * strength; bForce.y += dy / distance * strength
        }
      }
      edgePairs.forEach(({ source, target, rest }) => {
        const a = seedPositions.get(source), b = seedPositions.get(target)
        if (!a || !b) return
        const dx = b.x - a.x, dy = b.y - a.y, distance = Math.hypot(dx, dy) || 1
        const strength = Math.max(-10, Math.min(10, (distance - rest) * .075))
        const aForce = forces.get(source)!, bForce = forces.get(target)!
        aForce.x += dx / distance * strength; aForce.y += dy / distance * strength
        bForce.x -= dx / distance * strength; bForce.y -= dy / distance * strength
      })
      folderPositions.forEach((_, folder) => {
        const members = placementOrder.filter(node => node.path.split('/')[0] === folder)
        if (members.length < 2) return
        const center = members.reduce((sum, node) => {
          const point = seedPositions.get(node.path)!
          return { x: sum.x + point.x / members.length, y: sum.y + point.y / members.length }
        }, { x: 0, y: 0 })
        members.forEach(node => {
          const point = seedPositions.get(node.path)!, dx = center.x - point.x, dy = center.y - point.y
          const distance = Math.hypot(dx, dy) || 1
          if (distance < 440) return
          const force = forces.get(node.path)!
          force.x += dx / distance * Math.min(2.1, (distance - 440) * .009)
          force.y += dy / distance * Math.min(2.1, (distance - 440) * .009)
        })
      })
      placementOrder.forEach(node => {
        const point = seedPositions.get(node.path)!, force = forces.get(node.path)!
        const magnitude = Math.hypot(force.x, force.y) || 1
        const step = Math.min(11, magnitude)
        seedPositions.set(node.path, { x: point.x + force.x / magnitude * step, y: point.y + force.y / magnitude * step })
      })
    }

    /* Map API nodes (including isolates) and API edges directly — no generated topology. */
    const elements: ElementDefinition[] = [
      ...visibleNodes.map(n => {
        const segments = n.path.split('/')
        const isIndex = segments[segments.length - 1]?.toLowerCase() === 'index.md'
        return {
          data: {
            id: n.path,
            label: n.title || segments[segments.length - 1]?.replace(/\.md$/, '') || n.path,
            folder: segments[0],
            isolated: n.isolated,
            degree: nodeDegree.get(n.path) ?? 0,
          },
          position: shouldReuseSavedLayout ? savedLayout.positions[n.path] : seedPositions.get(n.path),
          classes: [
            `folder-${folderIndex.get(segments[0]) ?? 0}`,
            isIndex ? 'index-node' : 'note-node',
            isIndex && segments.length <= 2 ? 'root-index' : '',
            (nodeDegree.get(n.path) ?? 0) >= 5 ? 'hub-node' : '',
            pinnedPaths.has(n.path) ? 'pinned' : '',
          ].filter(Boolean).join(' '),
        }
      }),
      ...visibleEdges.map(e => ({
        data: { id: e.id, source: e.source, target: e.target, edge_type: e.edge_type, explicit: e.explicit },
      })),
    ]

    cy.current = cytoscape({
      container,
      elements,
      style: [
        {
          selector: 'node',
          style: {
            'shape': 'ellipse' as any,
            label: 'data(label)',
            'background-color': '#b8b8b8',
            color: '#dce9f0',
            'font-size': 10,
             'font-family': 'ui-sans-serif, sans-serif',
            'text-opacity': 0,
            'text-background-color': '#0b1117',
            'text-background-opacity': 0.9,
            'text-background-padding': 3,
            'text-background-shape': 'roundrectangle' as any,
            'text-margin-y': -14,
            'width': 'mapData(degree, 0, 18, 9, 20)',
            'height': 'mapData(degree, 0, 18, 9, 20)',
            'border-width': 1,
            'border-color': '#e5f5f3',
            'border-opacity': 0.72,
            'opacity': 0.94,
          },
        },
        ...folderOptions.map((folder, index) => ({ selector: `node.folder-${index}`, style: { 'background-color': FOLDER_PALETTE[index % FOLDER_PALETTE.length], 'border-color': '#e6fffa', 'border-width': 2 } as any })),
        { selector: 'node.index-node', style: { 'shape': 'diamond', 'width': 18, 'height': 18, 'border-width': 2, 'border-opacity': 0.95, 'text-opacity': 0, 'z-index': 100 } as any },
        { selector: 'node.root-index', style: { 'shape': 'hexagon', 'width': 25, 'height': 25, 'border-width': 2.5, 'text-opacity': 0.82, 'font-size': 8, 'font-weight': 600, 'text-margin-y': -22, 'text-background-opacity': 0.64 } as any },
        { selector: 'node.hub-node', style: { 'border-width': 2, 'border-opacity': 0.85 } as any },
        {
          selector: 'node[?isolated]',
          style: {
            'border-width': 1,
            'border-style': 'dashed' as any,
            'border-color': '#4a4d55',
            'opacity': 0.7,
          },
        },
        { selector: 'node.pinned', style: { 'border-width': 3, 'border-color': '#f2b84b' } as any },
        { selector: 'node.focused', style: { 'text-opacity': 1, 'font-size': 10, 'font-weight': 600, 'text-outline-width': 3, 'text-outline-color': '#0b1117', 'text-margin-y': -18, 'z-index': 9999 } as any },
        { selector: 'node.query-active', style: { 'background-color': '#59d9b1', 'border-width': 3, 'border-color': '#c9fff0', 'border-opacity': 1, 'opacity': 1 } as any },
        { selector: 'node.scan-active', style: { 'background-color': '#75cfc1', 'border-width': 3, 'border-color': '#d8fff7', 'border-opacity': 1, 'opacity': 1 } as any },
        { selector: 'node.proposal-active', style: { 'background-color': '#f2b84b', 'border-width': 3, 'border-color': '#fff0c7', 'border-opacity': 1, 'opacity': 1 } as any },
        { selector: 'node.write-active', style: { 'background-color': '#d99a36', 'border-width': 3, 'border-color': '#fff0c7', 'border-opacity': 1, 'opacity': 1 } as any },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'line-cap': 'round',
            'width': 1,
            'line-color': '#829eaa',
            'target-arrow-color': '#829eaa',
            'target-arrow-shape': 'none',
            'opacity': 0.45,
          } as any,
        },
        { selector: 'edge[edge_type = "links_to"]', style: { 'width': 1.15, 'line-color': '#9ab7c1', 'target-arrow-color': '#9ab7c1', 'opacity': 0.6 } },
        { selector: 'edge[edge_type = "contains"]', style: { 'width': 1, 'line-style': 'dotted', 'line-color': '#7c9a91', 'target-arrow-shape': 'none', 'opacity': 0.62 } },
        { selector: 'edge.path-active', style: { 'width': 2.5, 'line-style': 'dashed', 'line-dash-pattern': [9, 7], 'line-color': '#59d9b1', 'target-arrow-color': '#59d9b1', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, 'opacity': 1 } },
        { selector: 'edge.flow-scan', style: { 'line-color': '#75cfc1', 'target-arrow-color': '#75cfc1' } },
        { selector: 'edge.flow-proposal', style: { 'line-color': '#f2b84b', 'target-arrow-color': '#f2b84b' } },
        { selector: 'edge.flow-write', style: { 'line-color': '#d99a36', 'target-arrow-color': '#d99a36' } },
        { selector: 'edge.flow-reverse', style: { 'source-arrow-shape': 'triangle', 'source-arrow-color': '#59d9b1', 'target-arrow-shape': 'none' } },
        { selector: 'edge.flow-reverse.flow-scan', style: { 'source-arrow-color': '#75cfc1' } },
        { selector: 'edge.flow-reverse.flow-proposal', style: { 'source-arrow-color': '#f2b84b' } },
        { selector: 'edge.flow-reverse.flow-write', style: { 'source-arrow-color': '#d99a36' } },
        ...FOLDER_PALETTE.map((color, index) => ({ selector: `edge.flow-folder-${index}`, style: { 'line-color': color, 'target-arrow-color': color, 'source-arrow-color': color } })),
        ...FOLDER_PALETTE.map((color, index) => ({ selector: `node.transit-folder-${index}`, style: { 'border-width': 3, 'border-color': color, 'opacity': 1 } as any })),
      ],
      layout: { name: 'preset', fit: true, padding: shouldReuseSavedLayout ? 72 : 92 },
      minZoom: 0.3,
      maxZoom: 2,
    })

    const core = cy.current
    const syncAuras = () => {
      const container = ref.current
      const graphNodes = core.nodes() as any
      if (!container || typeof graphNodes.forEach !== 'function') return
      const viewport = { width: Math.max(1, container.clientWidth), height: Math.max(1, container.clientHeight) }
      setAuraViewport(current => current.width === viewport.width && current.height === viewport.height ? current : viewport)
      const groups = new Map<string, Position[]>()
      graphNodes.forEach((node: any) => {
        const folder = node.data('folder') as string
        const position = node.renderedPosition?.()
        if (!folder || !position) return
        groups.set(folder, [...(groups.get(folder) ?? []), position])
      })
      setGroupGas([...groups.entries()].filter(([, points]) => points.length > 1).map(([folder, points]) => {
        const index = folderIndex.get(folder) ?? 0
        return { id: `gas-${index}`, color: FOLDER_PALETTE[index % FOLDER_PALETTE.length], path: gasPath(points) }
      }))
    }
    core.on('layoutstop pan zoom dragfree', syncAuras)
    syncAuras()
    const auraTimer = window.setTimeout(syncAuras, 120)
    let lastDragPosition: { x: number; y: number } | null = null
    let activeDragNode: any | null = null
    let tensionFrame: number | null = null
    const springLengths = new Map<string, number>()
    const beginTension = (event: any) => {
      const node = event.target
      activeDragNode = node
      lastDragPosition = node.position()
      springLengths.clear()
      const remember = (edge: any) => {
        const source = edge.source().position(), target = edge.target().position()
        springLengths.set(edge.id(), Math.hypot(target.x - source.x, target.y - source.y))
      }
      node.connectedEdges?.().forEach(remember)
      node.neighborhood?.('node').connectedEdges?.().forEach(remember)
      if (tensionFrame === null) tensionFrame = window.requestAnimationFrame(runTension)
    }
    const runTension = () => {
      tensionFrame = null
      const node = activeDragNode
      if (!node) return
      const next = node.position()
      const previous = lastDragPosition
      lastDragPosition = next
      const dx = previous ? next.x - previous.x : 0, dy = previous ? next.y - previous.y : 0
      const affected = new Map<string, any>([[node.id(), node]])
      const move = (anchor: any, neighbour: any, edge: any, spring: number, carry: number) => {
        if (neighbour.id() === node.id() || neighbour.hasClass?.('pinned')) return
        const position = neighbour.position()
        const anchorPosition = anchor.position()
        const distanceX = position.x - anchorPosition.x, distanceY = position.y - anchorPosition.y
        const distance = Math.hypot(distanceX, distanceY) || 1
        const rest = springLengths.get(edge.id()) ?? distance
        /* A tensile spring only pulls when the link is stretched, then carries
           a little momentum so a whole connected constellation visibly follows. */
        const stretch = Math.max(0, Math.min(180, distance - rest))
        neighbour.position({
          x: position.x - distanceX / distance * stretch * spring + dx * carry,
          y: position.y - distanceY / distance * stretch * spring + dy * carry,
        })
        affected.set(neighbour.id(), neighbour)
      }
      const edges = node.connectedEdges?.()
      if (typeof edges?.forEach === 'function') core.batch(() => edges.forEach((edge: any) => {
          const neighbour = edge.source().id() === node.id() ? edge.target() : edge.source()
          move(node, neighbour, edge, 0.22, 0.18)
          const secondEdges = neighbour.connectedEdges?.()
          if (typeof secondEdges?.forEach !== 'function') return
          secondEdges.forEach((secondEdge: any) => {
            const second = secondEdge.source().id() === neighbour.id() ? secondEdge.target() : secondEdge.source()
            move(neighbour, second, secondEdge, 0.09, 0.045)
          })
        }))
      /* Keep the responsive physics local to the dragged constellation. Every
         nearby star gets a tiny shove instead of allowing two nodes to occupy
         the same visual space; nothing runs while the map is idle. */
      core.batch(() => affected.forEach(anchor => {
        const anchorPosition = anchor.position()
        core.nodes().forEach((other: any) => {
          if (other.id() === anchor.id() || other.id() === node.id() || other.hasClass?.('pinned')) return
          const position = other.position()
          let distanceX = position.x - anchorPosition.x, distanceY = position.y - anchorPosition.y
          let distance = Math.hypot(distanceX, distanceY)
          if (distance >= 42) return
          if (distance < .01) { distanceX = Math.random() - .5; distanceY = Math.random() - .5; distance = Math.hypot(distanceX, distanceY) }
          const nudge = Math.min(2.4, (42 - distance) * .14)
          other.position({ x: position.x + distanceX / distance * nudge, y: position.y + distanceY / distance * nudge })
        })
      }))
      tensionFrame = window.requestAnimationFrame(runTension)
    }
    const endTension = () => {
      activeDragNode = null
      if (tensionFrame !== null) window.cancelAnimationFrame(tensionFrame)
      tensionFrame = null
      lastDragPosition = null
      springLengths.clear()
      syncAuras()
    }
    core.on('grab', 'node', beginTension)
    core.on('dragfree', 'node', endTension)

    cy.current.on('tap', 'node', (e: any) => {
      const node = e.target
      setSelectedPath(node.id())
      cy.current?.nodes().removeClass('focused')
      node.addClass('focused')
       if (!prefersReducedMotion()) node.animate({ position: { x: node.position('x') + 5, y: node.position('y') + 5 }, duration: 100 }, () => {
         node.animate({ position: { x: node.position('x') - 5, y: node.position('y') - 5 }, duration: 100 })
       })
    })

    /* Replay events that arrived before this core existed, then replay the most
       recent flow activity so a refresh rebuild re-highlights the relevant
       nodes — including nodes that only exist in the new snapshot. */
    const queued = pendingEvents.current
    pendingEvents.current = []
    queued.forEach(q => applyActivity(q))
    if (lastFlowEvent.current) applyActivity(lastFlowEvent.current)

    return () => {
      window.clearTimeout(auraTimer)
      if (tensionFrame !== null) window.cancelAnimationFrame(tensionFrame)
      if (typeof (core as any).off === 'function') core.off('layoutstop pan zoom dragfree', syncAuras)
      if (typeof (core as any).off === 'function') core.off('grab dragfree', 'node')
      if (cy.current === core) cy.current = null
      /* During a reflow, let any in-flight pointer event finish before the
         renderer is destroyed. On a real unmount, release it immediately. */
      if (unmounting.current) core.destroy()
      else window.setTimeout(() => core.destroy(), 0)
    }
    /* Rebuild only when the snapshot generation changes, never per activity event. */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation, applyActivity])

  /* --- Manual rescan --- */
  const handleRescan = async () => {
    setScanning(true)
    setRescanError('')
    try {
      const prev = await api<Status>('/api/v1/status').then(s => s.last_scan_completed_at).catch(() => null)
      await api('/api/v1/scan', { method: 'POST' })
      let completed = false
      for (let attempts = 0; attempts < 60; attempts++) {
        const s = await api<Status>('/api/v1/status')
        if (s.last_scan_completed_at !== prev) { completed = true; break }
        if (!alive.current) return
        await new Promise(r => setTimeout(r, 1000))
      }
      if (!alive.current) return
      if (!completed) { setRescanError('scan did not complete in time'); return }
      onRefresh?.()
      loadGraph('refresh')
    } catch (e) {
      setRescanError(e instanceof Error ? e.message : 'rescan failed')
    } finally {
      setScanning(false)
    }
  }

  /* --- Selection: click, or keyboard navigation inside the graph container --- */
  const nodes = visibleNodes
  const edges = visibleEdges
  const hasNodes = nodes.length > 0
  const selectedNode = nodes.find(n => n.path === selectedPath) ?? null
  const connections = selectedPath ? edges.filter(ed => ed.source === selectedPath || ed.target === selectedPath).length : 0
  useEffect(() => {
    const core = cy.current
    if (!core) return
    core.nodes().removeClass('focused')
    if (selectedPath) core.getElementById(selectedPath).addClass('focused')
  }, [generation, selectedPath])
  const saveLayout = () => {
    const core = cy.current
    if (!core) return
    const positions: GraphLayoutState['positions'] = {}
    core.nodes().forEach(node => { positions[node.id()] = node.position() })
    writeGraphLayout({ positions, pinned: [...pinnedPaths] })
    setLayoutNotice('Map layout saved on this device.')
  }
  const resetLayout = () => {
    try { localStorage.removeItem(GRAPH_LAYOUT_KEY) } catch { /* Storage is optional. */ }
    setPinnedPaths(new Set())
    setLayoutEpoch(previous => previous + 1)
    setLayoutNotice('Saved map layout cleared. Refresh to reflow the map.')
  }
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await graphCardRef.current?.requestFullscreen()
    } catch { /* Fullscreen may be unavailable in an embedded browser. */ }
  }
  useEffect(() => {
    const syncFullscreen = () => {
      const fullscreen = document.fullscreenElement === graphCardRef.current
      setIsGraphFullscreen(fullscreen)
      window.setTimeout(() => {
        const core = cy.current as any
        core?.resize?.()
        if (fullscreen) core?.fit?.(undefined, 72)
      }, 80)
    }
    document.addEventListener('fullscreenchange', syncFullscreen)
    return () => document.removeEventListener('fullscreenchange', syncFullscreen)
  }, [])
  const togglePin = () => {
    if (!selectedPath) return
    setPinnedPaths(previous => {
      const next = new Set(previous)
      if (next.has(selectedPath)) next.delete(selectedPath)
      else next.add(selectedPath)
      const node = cy.current?.getElementById(selectedPath)
      if (next.has(selectedPath)) node?.addClass('pinned')
      else node?.removeClass('pinned')
      writeGraphLayout({ ...readGraphLayout(), pinned: [...next] })
      return next
    })
  }

  const moveSelection = useCallback((delta: number) => {
    const list = visibleNodes
    if (!list.length) return
    setSelectedPath(prev => {
      const idx = prev ? list.findIndex(n => n.path === prev) : -1
      /* No current selection: forward keys start at the first node,
         backward keys start at the last one. */
      const next = idx === -1 ? (delta > 0 ? 0 : list.length - 1) : (idx + delta + list.length) % list.length
      return list[next].path
    })
  }, [visibleNodes])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    const list = visibleNodes
    if (!list.length) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1) }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1) }
    else if (e.key === 'Home') { e.preventDefault(); setSelectedPath(list[0].path) }
    else if (e.key === 'End') { e.preventDefault(); setSelectedPath(list[list.length - 1].path) }
    else if (e.key === 'Escape') { e.preventDefault(); setSelectedPath(null) }
  }, [moveSelection, visibleNodes])

  /* Accessible label for the canvas: state, counts, and how to operate it. */
  const graphLabel =
    phase === 'loading'
      ? 'Knowledge graph loading notes.'
      : phase === 'error'
        ? `Knowledge graph unavailable. ${graphError}. Use the retry button to load again.`
        : !hasNodes
          ? 'Knowledge graph with no indexed notes yet.'
          : `Knowledge graph with ${nodes.length} ${nodes.length === 1 ? 'note' : 'notes'} and ${edges.length} ${edges.length === 1 ? 'link' : 'links'}. Use arrow keys to move between notes and Escape to clear.${selectedNode ? ` Selected: ${selectedNode.title}, ${selectedNode.path}.` : ''}`

  return (
    <div className="content graph-screen">
      <Box className="graph-header screen-intro" sx={{ display: 'flex', alignItems: 'flex-end', gap: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flex: 1, minWidth: 240 }}>
          <Typography variant="overline" color="text.secondary">MEMORY CHART · LOCAL INDEX</Typography>
          <Typography variant="h5">{hasNodes ? `${nodes.length} notes` : '…'}</Typography>
          <Typography variant="body2" color="text.secondary">This Harbor Ledger server keeps its vault local. Teal routes show reads; amber marks attention and approval.</Typography>
        </Box>
        <Box className="graph-controls" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <span className="live-indicator"><i /> {scanning ? 'scanning…' : 'live'}</span>
          <Button onClick={handleRescan} disabled={scanning}>{scanning ? 'rescanning…' : 'rescan'}</Button>
          {rescanError && <Typography className="rescan-error" variant="body2" color="error.main" role="alert">{rescanError}</Typography>}
        </Box>
      </Box>
      <Box className="graph-workspace-toolbar" aria-label="Graph workspace controls" sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', p: 1.25, border: 1, borderColor: 'divider', borderRadius: 2, bgcolor: 'background.paper' }}>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 1 }}>Folder
          <Select native size="small" aria-label="Filter graph by folder" value={folderScope} onChange={e => { setFolderScope(e.target.value); setSelectedPath(null) }}>
            <option value="all">All folders</option>
            {folderOptions.map(folder => <option key={folder} value={folder}>{folder}</option>)}
          </Select>
        </label>
        <FormControlLabel className="graph-toggle" control={<Checkbox checked={showDisconnected} onChange={e => setShowDisconnected(e.target.checked)} />} label={`show ${disconnectedCount} unlinked`} />
        <span className="graph-flow-state"><i />activity connected</span>
        <Box className="graph-layout-actions" sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
          <Button size="small" variant="outlined" onClick={saveLayout}>save chart</Button>
          <Button size="small" variant="outlined" onClick={resetLayout}>reset chart</Button>
          <Button size="small" variant="outlined" onClick={toggleFullscreen} aria-label="View memory chart fullscreen">fullscreen</Button>
        </Box>
      </Box>
      <div ref={graphCardRef} className="graph-card" style={{ position: 'relative' }}>
        {isGraphFullscreen && <Button className="graph-fullscreen-exit" size="small" onClick={toggleFullscreen}>exit fullscreen</Button>}
        <svg className="graph-group-auras" aria-hidden="true" viewBox={`0 0 ${auraViewport.width} ${auraViewport.height}`} preserveAspectRatio="none">
          <defs>
            <filter id="group-gas-wide" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="19" /></filter>
            <filter id="group-gas-core" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="7" /></filter>
          </defs>
          {groupGas.map(gas => <g key={gas.id} color={gas.color}>
            <path className="group-gas-wide" d={gas.path} filter="url(#group-gas-wide)" />
            <path className="group-gas-core" d={gas.path} filter="url(#group-gas-core)" />
          </g>)}
        </svg>
        <div
          ref={ref}
          className="cytoscape"
          tabIndex={0}
          role="application"
          aria-label={graphLabel}
          aria-busy={phase === 'loading'}
          onKeyDown={handleKeyDown}
        />
        {phase === 'loading' && (
          <Box className="graph-loading" role="status" sx={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
            <CircularProgress size={28} />
            <Typography variant="body2" color="text.secondary">indexing notes…</Typography>
          </Box>
        )}
        {phase === 'error' && (
          <Box className="graph-empty" role="alert" sx={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
            <Typography variant="body2" color="error.main">{graphError || 'graph unavailable'}</Typography>
            <Button onClick={() => loadGraph('initial')}>retry</Button>
          </Box>
        )}
        {phase === 'ready' && !hasNodes && (
          <Box className="graph-empty" sx={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
            <CircularProgress size={28} />
            <Typography variant="body2" color="text.secondary">no indexed notes yet</Typography>
          </Box>
        )}
        {staleError && phase === 'ready' && (
          <Box className="graph-stale" role="status" sx={{ position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)', display: 'flex', alignItems: 'center', gap: 1.5, p: 1, border: 1, borderColor: 'warning.main', bgcolor: 'warning.light', borderRadius: 1.5 }}>
            <Typography variant="body2">live update stale</Typography>
            <Button size="small" onClick={() => loadGraph('refresh')}>retry</Button>
          </Box>
        )}
        {hasNodes && (
          <div className="graph-legend">
            <span className="legend-item"><span className="legend-dot memory" /> note</span>
            <span className="legend-item"><span className="legend-diamond" /> index note</span>
            <span className="legend-item"><span className="legend-line solid" /> wiki link</span>
            <span className="legend-item"><span className="legend-line dotted" /> folder relation</span>
            <span className="legend-item legend-read"><span className="legend-dot query" /> read</span>
            <span className="legend-item legend-index"><span className="legend-dot scan" /> scan</span>
            <span className="legend-item legend-propose"><span className="legend-dot proposal" /> propose</span>
            <span className="legend-item legend-write"><span className="legend-dot write" /> write</span>
            <span className="graph-flow-hint">arrows show the live direction</span>
          </div>
        )}
        {selectedNode && (
          <section className="node-details" aria-label="Selected note details">
            <Box className="node-details-head" sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              <Typography variant="h6" sx={{ flex: 1, minWidth: 160 }}>{selectedNode.title}</Typography>
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button size="small" variant={pinnedPaths.has(selectedNode.path) ? 'contained' : 'outlined'} onClick={togglePin}>{pinnedPaths.has(selectedNode.path) ? 'unpin' : 'pin'}</Button>
                <Button size="small" onClick={() => setSelectedPath(null)}>close</Button>
              </Box>
            </Box>
            <dl className="node-details-list">
              <div><dt>path</dt><dd>{selectedNode.path}</dd></div>
              <div><dt>links</dt><dd>{connections}</dd></div>
              {selectedNode.isolated && <div><dt>state</dt><dd>isolated</dd></div>}
            </dl>
          </section>
        )}
        {layoutNotice && <Box className="graph-layout-notice" role="status" sx={{ position: 'absolute', bottom: 12, left: 12, p: 1, border: 1, borderColor: 'divider', borderRadius: 1.5, bgcolor: 'background.paper' }}>{layoutNotice}</Box>}
        <span className="sr-only" role="status" aria-live="polite">
          {selectedNode ? `${selectedNode.title} selected. ${selectedNode.path}` : ''}
        </span>
      </div>
    </div>
  )
}

/* --- Activity row helper --- */
function ActivityRow({ event }: { event: Activity }) {
  const activityDescription = (e: Activity) => {
    const payload = e.payload
    if (e.event_type === 'scan') return `${payload.files_indexed ?? 0} notes indexed · ${payload.diagnostics ?? 0} diagnostics`
    if (e.event_type === 'query') return `retrieval trail · ${payload.selected_count ?? 0} memories selected`
    return Object.keys(payload).length ? `${Object.keys(payload).join(', ')} updated` : 'system signal received'
  }
  return (
    <div className="activity-row">
      <span className={`event-icon ${event.event_type}`}>
        {event.event_type === 'query' ? '⌕' : event.event_type === 'scan' ? '◌' : '↗'}
      </span>
      <div>
        <strong>{label(event.event_type)}</strong>
        <p>{activityDescription(event)}</p>
      </div>
      <time>{timeAgo(event.created_at)}</time>
    </div>
  )
}

/* --- Settings screen --- */
export type WriteProposal = {
  id: number
  path: string
  content: string
  operation: string
  status: string
  rule_access: string
  requested_at: string
  resolved_at: string | null
  failure_reason: string | null
}

/* Backend status surfaced as a review nudge. */
const statusLabel = (status: string) => (status === 'reconciliation_required' ? 'needs review' : label(status))
/* Newline-safe preview: one line, capped, ellipsis. React escapes on render. */
const previewContent = (content: string) => {
  const flat = content.replace(/\s+/g, ' ').trim()
  return flat.length > 160 ? `${flat.slice(0, 160).trimEnd()}…` : flat
}

export function Settings() {
  const [settings, setSettings] = useState<any>(null)
  const [settingsError, setSettingsError] = useState('')
  const [fallbackFolders, setFallbackFolders] = useState<string[]>([])
  const [rules, setRules] = useState<any[]>([])
  const [expanded, setExpanded] = useState(new Set(['.']))
  const [selectedFolder, setSelectedFolder] = useState<string | null>(null)
  const [folderFilter, setFolderFilter] = useState('')
  const [writes, setWrites] = useState<WriteProposal[] | null>(null)
  const [writesError, setWritesError] = useState('')
  const [writesStale, setWritesStale] = useState('')
  const [busy, setBusy] = useState<{ id: number; action: 'approve' | 'reject' } | null>(null)
  const [actionErrors, setActionErrors] = useState<Record<number, string>>({})
  const [liveMessage, setLiveMessage] = useState('')
  const [newFolderPath, setNewFolderPath] = useState('')
  const [creatingFolder, setCreatingFolder] = useState(false)
  const [folderCreateError, setFolderCreateError] = useState('')
  const [tokenRows, setTokenRows] = useState<TokenSummary[] | null>(null)
  const [tokenName, setTokenName] = useState('')
  const [tokenAdmin, setTokenAdmin] = useState(false)
  const [approveOwnProposals, setApproveOwnProposals] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [justCreated, setJustCreated] = useState<{ name: string; token: string } | null>(null)
  const [revealed, setRevealed] = useState(false)
  const [copied, setCopied] = useState(false)
  /* 403 on GET /tokens is the non-admin signal: the stored token works, but it
     cannot manage tokens. */
  const [adminDenied, setAdminDenied] = useState(false)
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null)
  const [tokenError, setTokenError] = useState('')
  const confirmTimer = useRef<number | null>(null)

  useEffect(() => {
    api<any>('/api/v1/settings').then(r => {
      setSettings(r)
      setRules(r.folder_rules || [])
      /* Older running services do not expose `folders` yet. Derive a useful
         tree from the graph so the settings page never collapses to an empty
         vault while the service is being upgraded. */
      if (!Array.isArray(r.folders) || r.folders.length === 0) {
        api<GraphSnapshot>('/api/v1/graph').then(graph => {
          const paths = new Set<string>()
          graph.nodes.forEach(node => {
            const parts = node.path.split('/').filter(Boolean)
            for (let index = 1; index < parts.length; index++) paths.add(parts.slice(0, index).join('/'))
          })
          setFallbackFolders([...paths].sort())
        }).catch(() => undefined)
      }
    }).catch(e => setSettingsError(e instanceof Error ? e.message : 'vault permissions unavailable'))
  }, [])

  /* Proposals this session resolved via POST — guards against an older list
     snapshot re-exposing them as pending (and thus actionable) again. */
  const resolvedWrites = useRef(new Map<number, WriteProposal>())

  const applyWrites = (list: WriteProposal[]) => {
    setWrites(list.map(w => {
      const local = resolvedWrites.current.get(w.id)
      return local && w.status === 'pending' ? local : w
    }))
  }

  const loadWrites = (silent = false) => {
    if (!silent) { setWrites(null); setWritesStale('') }
    setWritesError('')
    return api<{ proposals: WriteProposal[] }>('/api/v1/writes')
      .then(r => { applyWrites(r.proposals ?? []); setWritesStale('') })
      .catch(e => {
        const message = e instanceof Error ? e.message : 'failed to load writes'
        if (silent) setWritesStale(message)
        else setWritesError(message)
      })
  }

  useEffect(() => { loadWrites() }, [])

  const refreshTokens = () =>
    api<TokenSummary[]>('/api/v1/tokens')
      .then(r => { setTokenRows(Array.isArray(r) ? r : []); setTokenError('') })
      .catch(e => {
        /* A 403 here is the non-admin signal, not a list failure: the token is
           valid but may not manage tokens. */
        if (e instanceof APIError && e.status === 403) { setAdminDenied(true); setTokenRows(null); setTokenError(''); return }
        setTokenRows([]); setTokenError(e instanceof Error ? e.message : 'failed to load tokens')
      })

  useEffect(() => { refreshTokens() }, [])

  /* Generate: persist the folder draft as the next token's template, then mint
     the token with that draft as its own rules. */
  const generateToken = async () => {
    const name = tokenName.trim() || tokenNameSuggestion()
    setGenerating(true); setTokenError(''); setJustCreated(null)
    try {
      await api('/api/v1/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_rules: rules }) })
      const r = await api<{ token: string } & TokenSummary>('/api/v1/tokens', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, rules, admin: tokenAdmin, approve_own_proposals: approveOwnProposals }) })
      setJustCreated({ name, token: r.token })
      setRevealed(false); setCopied(false)
      setTokenName('')
      refreshTokens()
    } catch (e) { setTokenError(e instanceof Error ? e.message : 'failed to create token') }
    finally { setGenerating(false) }
  }

  const copyToken = async () => {
    if (!justCreated) return
    const text = justCreated.token
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text)
      else {
        const el = document.createElement('textarea'); el.value = text; document.body.appendChild(el); el.select()
        document.execCommand('copy'); el.remove()
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2500)
    } catch { setTokenError('copy failed — select the token and copy it manually') }
  }

  const armRevoke = (name: string) => {
    setConfirmRevoke(name)
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current)
    confirmTimer.current = window.setTimeout(() => setConfirmRevoke(null), 4000)
  }

  const revokeToken = async (name: string) => {
    setConfirmRevoke(null)
    setTokenError('')
    try { await api(`/api/v1/tokens/${encodeURIComponent(name)}`, { method: 'DELETE' }); refreshTokens() }
    catch (e) { setTokenError(e instanceof Error ? e.message : 'failed to revoke token') }
  }

  /* Persist the folder-permission draft for the browser's own session. */
  const saveSettings = async () => {
    setSettingsError('')
    try {
      await api('/api/v1/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder_rules: rules }) })
    } catch (e) { setSettingsError(e instanceof Error ? e.message : 'failed to save folder settings') }
  }

  const resolveProposal = async (action: 'approve' | 'reject', proposal: WriteProposal) => {
    setBusy({ id: proposal.id, action })
    setActionErrors(prev => { const next = { ...prev }; delete next[proposal.id]; return next })
    try {
      const resolved = await api<WriteProposal>(`/api/v1/writes/${proposal.id}/${action}`, { method: 'POST' })
      setLiveMessage(action === 'approve' ? `${proposal.path} approved and written to the vault.` : `${proposal.path} rejected. The vault was not modified.`)
      /* Trust the POST response: move the proposal into the audit immediately so
         a failed (stale) refresh can never re-expose it as actionable. */
      resolvedWrites.current.set(resolved.id, resolved)
      setWrites(prev => (prev ? prev.map(w => (w.id === resolved.id ? resolved : w)) : prev))
      /* Keep the card busy until the refresh lands so a stale card cannot be re-submitted. */
      await loadWrites(true)
    } catch (e) {
      const message = e instanceof Error ? e.message : 'the action failed'
      setActionErrors(prev => ({ ...prev, [proposal.id]: message }))
      setLiveMessage(`${proposal.path}: ${action === 'approve' ? 'approval' : 'rejection'} failed.`)
    } finally {
      setBusy(prev => (prev && prev.id === proposal.id ? null : prev))
    }
  }

  const proposeFolder = async () => {
    const path = newFolderPath.trim()
    if (!path) { setFolderCreateError('Enter a folder path.'); return }
    setCreatingFolder(true); setFolderCreateError('')
    try {
      await api<WriteProposal>('/api/v1/writes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, content: '', operation: 'mkdir' }),
      })
      setNewFolderPath('')
      await loadWrites(true)
    } catch (error) {
      setFolderCreateError(error instanceof Error ? error.message : 'failed to create folder proposal')
    } finally { setCreatingFolder(false) }
  }

  const toggleExpand = (path: string) => {
    setExpanded(prev => { const next = new Set(prev); if (next.has(path)) next.delete(path); else next.add(path); return next })
  }

  type FolderNode = { path: string; name: string; children: FolderNode[] }
  const buildTree = (paths: string[]): FolderNode => {
    const root: FolderNode = { path: '.', name: 'vault', children: [] }
    paths.forEach(path => {
      if (!path || path === '.') return
      const parts = path.split('/').filter(Boolean)
      let current: any[] = root.children
      parts.forEach((part: string, idx: number) => {
        const key = parts.slice(0, idx + 1).join('/')
        const existing = current.find(c => c.path === key)
        if (existing) { current = existing.children }
        else {
          const newNode: FolderNode = { path: key, name: part, children: [] }
          current.push(newNode)
          current = newNode.children
        }
      })
    })
    const sort = (node: FolderNode) => { node.children.sort((a, b) => a.name.localeCompare(b.name)); node.children.forEach(sort) }
    sort(root)
    return root
  }

  const explicitRule = (path: string) => rules.find(rule => rule.path === path)
  const accessFor = (path: string) => {
    const match = rules.filter(rule => rule.path === '.' || path === rule.path || path.startsWith(`${rule.path}/`))
      .sort((a, b) => b.path.length - a.path.length)[0]
    return match?.access ?? 'read'
  }
  const setFolderAccess = (path: string, access: string) => {
    if (!path) return
    const current = explicitRule(path)
    setRules(current
      ? rules.map(rule => rule.path === path ? { ...rule, access } : rule)
      : [...rules, { path, access }])
  }

  const TreeItem = ({ node, depth }: { node: FolderNode, depth: number }) => {
    const expandedNode = expanded.has(node.path)
    const explicit = node.path === '.' ? null : explicitRule(node.path)
    const access = accessFor(node.path)
    const query = folderFilter.trim().toLowerCase()
    const matches = !query || node.path.toLowerCase().includes(query) || treePaths(node.children).some(path => path.toLowerCase().includes(query))
    if (!matches && node.path !== '.') return null
    return (
      <Box sx={{ pl: depth * 1.5 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 1, borderRadius: 1.5, bgcolor: selectedFolder === node.path ? 'action.selected' : 'transparent', flexWrap: 'wrap' }}>
          {node.children.length > 0 && (
            <Button size="small" sx={{ minWidth: 34, px: 0.5, fontSize: 11, color: 'text.secondary' }} aria-label={`${expandedNode ? 'Collapse' : 'Expand'} ${node.name}`} onClick={() => toggleExpand(node.path)}>
              {expandedNode ? '▼' : '▶'}
            </Button>
          )}
          <Button size="small" onClick={() => setSelectedFolder(node.path)} aria-pressed={selectedFolder === node.path} sx={{ gap: 0.5, px: 1, textAlign: 'left', whiteSpace: 'nowrap', fontWeight: selectedFolder === node.path ? 600 : 400 }}>
            <Typography component="span" variant="body2" aria-hidden>{node.path === '.' ? '⌂' : '▰'}</Typography>
            {node.name}
          </Button>
          <Chip size="small" variant={explicit ? 'filled' : 'outlined'} sx={{ fontSize: 11, height: 22 }} label={`${explicit ? 'set' : 'inherits'} · ${access}`} />
          <Select
            native
            size="small"
            aria-label={`Quick permission for ${node.path}`}
            value={access}
            onClick={e => e.stopPropagation()}
            onChange={e => setFolderAccess(node.path, e.target.value)}
            sx={{ minWidth: 128, fontSize: 13 }}
          >
            <option value="read">read</option>
            <option value="none">none</option>
            <option value="propose-write">ask to write</option>
            <option value="auto-write">allow writes</option>
          </Select>
        </Box>
        {expandedNode && node.children.map(child => <TreeItem node={child} depth={depth + 1} key={child.path} />)}
      </Box>
    )
  }

  const discoveredFolders = useMemo(() => {
    const configured: string[] = Array.isArray(settings?.folders) && settings.folders.length > 0
      ? settings.folders.filter((path: unknown): path is string => typeof path === 'string')
      : fallbackFolders
    const paths = configured.length > 0 ? configured : rules.map(rule => rule.path)
    return [...new Set(paths.filter((path: string) => path && path !== '.'))]
  }, [fallbackFolders, settings?.folders, rules])
  const tree = buildTree(discoveredFolders)
  const treePaths = (paths: FolderNode[]): string[] => paths.flatMap(node => [node.path, ...treePaths(node.children)])
  const selectedRule = selectedFolder ? explicitRule(selectedFolder) : null
  const inheritingDescendants = selectedFolder
    ? treePaths(tree.children).filter(path => (selectedFolder === '.' || path === selectedFolder || path.startsWith(`${selectedFolder}/`)) && !rules.some(rule => rule.path !== selectedFolder && (rule.path === '.' || path === rule.path || path.startsWith(`${rule.path}/`))))
    : []
  const pending = (writes ?? []).filter(w => w.status === 'pending')
  const audit = (writes ?? []).filter(w => w.status !== 'pending')
  const pendingHeading = pending.length === 0 ? 'Proposals' : `${pending.length} ${pending.length === 1 ? 'proposal' : 'proposals'} awaiting approval`

  return (
    <Box component="section" sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Box sx={{ mb: 1 }}>
        <Typography variant="h2">Vault Permissions</Typography>
        <Typography variant="body1" color="text.secondary">Your vault stays local. Choose exactly what trusted agents can read or write, with every change reviewable.</Typography>
      </Box>
      <Paper elevation={0} sx={{ p: { xs: 2, md: 3 }, border: 1, borderColor: 'divider' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', mb: 2 }}>
          <Box>
            <Typography variant="overline" color="text.secondary">VAULT</Typography>
            <Typography variant="h6">{settings?.vault_path || 'Vault permissions'}</Typography>
          </Box>
          <Box sx={{ ml: 'auto' }}>
            <Chip size="small" label={`${discoveredFolders.length} folders`} />
          </Box>
        </Box>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, border: 1, borderColor: 'divider', borderRadius: 2, p: 2, bgcolor: 'background.default' }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Typography variant="body2" color="text.secondary">{discoveredFolders.length} folders + vault root · {rules.length} overrides</Typography>
            <Box sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
              <Button size="small" onClick={() => setExpanded(new Set(treePaths(tree.children).concat('.')))}>expand all</Button>
              <Button size="small" onClick={() => setExpanded(new Set(['.']))}>collapse</Button>
            </Box>
          </Box>
          <TextField
            size="small"
            sx={{ maxWidth: 320 }}
            slotProps={{ input: { 'aria-label': 'Filter vault folders' } }}
            value={folderFilter}
            onChange={e => setFolderFilter(e.target.value)}
            placeholder="Filter folders…"
          />
          <Typography variant="body2" color="text.secondary">Select a folder to inspect its inherited rule, or set access directly from its row.</Typography>
          <Box sx={{ minHeight: 40 }}>
            {settingsError ? <Box role="alert" sx={{ display: 'flex', alignItems: 'center', gap: 2, color: 'error.main' }}>{settingsError}<Button size="small" onClick={() => window.location.reload()}>retry</Button></Box> : <TreeItem node={tree} depth={0} />}
          </Box>
          {selectedFolder && (
            <Box sx={{ p: 2, border: 1, borderColor: 'divider', borderRadius: 2, bgcolor: 'background.paper' }}>
              <Typography variant="body2" sx={{ fontFamily: '"IBM Plex Mono", monospace', color: 'text.secondary', mb: 1 }}>{selectedFolder === '.' ? 'vault root' : selectedFolder}</Typography>
              <Typography variant="body2" sx={{ display: 'block', mb: 0.5 }}>Permission for {selectedFolder === '.' ? 'vault root' : selectedFolder}</Typography>
              <Select native size="small" aria-label={`Permission for ${selectedFolder}`} value={selectedRule?.access ?? accessFor(selectedFolder)} onChange={e => setFolderAccess(selectedFolder, e.target.value)} sx={{ minWidth: 160, mb: 1 }}>
                <option value="read">read</option>
                <option value="none">none</option>
                <option value="propose-write">propose-write</option>
                <option value="auto-write">auto-write</option>
              </Select>
              {selectedRule
                ? <Button size="small" color="error" onClick={() => setRules(rules.filter(rule => rule.path !== selectedFolder))}>remove explicit override</Button>
                : <Button size="small" disabled={selectedFolder === '.'} onClick={() => setFolderAccess(selectedFolder, accessFor(selectedFolder))}>save inherited value</Button>}
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                {selectedRule ? (selectedRule.access === 'read' ? 'Read-only — writes create a proposal instead of touching the vault.' : selectedRule.access === 'none' ? 'No access — matching notes are hidden from this token.' : selectedRule.access === 'propose-write' ? 'Read access with write proposals requiring approval.' : 'Read and write access, applied immediately with an audit record.') : 'This folder currently inherits read access from its nearest explicit rule.'}
                {inheritingDescendants.length > 0 && ` Applies to ${inheritingDescendants.length} descendant folder${inheritingDescendants.length === 1 ? '' : 's'} without an explicit override.`}
              </Typography>
            </Box>
          )}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
            <Typography variant="body2" color="text.secondary">{settings?.vault_path || 'no vault configured'}</Typography>
            <Button variant="contained" size="small" sx={{ ml: 'auto' }} disabled={!settings} onClick={saveSettings}>save folder settings</Button>
          </Box>
        </Box>
      </Paper>
      <div className="settings-workspace-layout" data-layout="quiet-split">
      <section className="tokens-section" aria-labelledby="tokens-heading">
        <Box className="tokens-intro" sx={{ mb: 3 }}>
          <Typography variant="overline" color="text.secondary">TRUSTED CONNECTIONS</Typography>
          <Typography variant="h2" id="tokens-heading">External access</Typography>
          <Typography variant="body2" color="text.secondary">The Web UI uses its own authenticated session. Tokens for trusted REST and MCP clients use the folder-permission policy configured above.</Typography>
        </Box>
        <div className="tokens-layout" data-layout="single-column">
          <Paper elevation={0} className="token-zone token-create" sx={{ p: { xs: 2, md: 2.5 }, border: 1, borderColor: 'divider' }} aria-labelledby="create-token-heading">
            <Typography variant="overline" color="text.secondary">CREATE TOKEN</Typography>
            <Typography variant="h6" id="create-token-heading">New external token</Typography>
            {adminDenied ? (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>This local session cannot manage external tokens — ask an admin to generate or revoke them.</Typography>
            ) : (
              <Box sx={{ display: 'grid', gap: 2, mt: 2 }}>
                <TextField size="small" label="Token name" name="token-name" autoComplete="off" value={tokenName} onChange={e => setTokenName(e.target.value)} placeholder={`${tokenNameSuggestion()}…`} inputProps={{ spellCheck: false }} />
                <FormControlLabel control={<Checkbox name="token-admin" checked={tokenAdmin} onChange={e => setTokenAdmin(e.target.checked)} />} label="Admin — can manage tokens" />
                <FormControlLabel control={<Checkbox name="approve-own-proposals" checked={approveOwnProposals} onChange={e => setApproveOwnProposals(e.target.checked)} />} label="Approve own proposals" />
                <Typography variant="caption" color="text.secondary">Admin tokens can create and revoke other tokens. Only enable this for a trusted operator.</Typography>
                <Button variant="contained" sx={{ alignSelf: 'flex-start' }} onClick={generateToken} disabled={generating} aria-busy={generating} startIcon={generating ? <CircularProgress size={14} /> : undefined}>Generate token</Button>
              </Box>
            )}
            {tokenError && <Alert severity="error" sx={{ mt: 2 }}>{tokenError}</Alert>}
          </Paper>
          {justCreated && <Paper elevation={0} className="token-zone token-revealed" sx={{ p: { xs: 2, md: 2.5 }, mt: 2, border: 1, borderColor: 'success.main', bgcolor: 'success.light', color: 'text.primary' }} aria-labelledby="token-revealed-heading">
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              <Typography variant="h6" id="token-revealed-heading">Token ready to copy</Typography>
              <Typography variant="caption" sx={{ ml: 'auto', color: 'text.secondary', fontVariant: 'small-caps', letterSpacing: '.08em' }}>shown only once</Typography>
            </Box>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1, mb: 2 }}>Copy this token now. It will not be available again after this session.</Typography>
            <Box role="status" sx={{ display: 'flex', gap: 2, alignItems: 'center', flexWrap: 'wrap' }}>
              <Box component="code" translate="no" sx={{ fontFamily: '"IBM Plex Mono", monospace', fontSize: 14, fontWeight: 600 }}>{revealed ? justCreated.token : '•'.repeat(Math.min(32, Math.max(16, justCreated.token.length)))}</Box>
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button size="small" variant="contained" onClick={copyToken}>{copied ? 'Copied ✓' : 'Copy token'}</Button>
                <Button size="small" variant="outlined" onClick={() => setRevealed(v => !v)}>{revealed ? 'Hide token' : 'Reveal token'}</Button>
              </Box>
            </Box>
          </Paper>}
          <Paper elevation={0} className="token-zone token-existing" sx={{ p: { xs: 2, md: 2.5 }, mt: 2, border: 1, borderColor: 'divider' }} aria-labelledby="existing-tokens-heading">
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              <Box>
                <Typography variant="overline" color="text.secondary">MANAGE</Typography>
                <Typography variant="h6" id="existing-tokens-heading">Existing tokens</Typography>
              </Box>
              {tokenRows !== null && <Chip size="small" label={tokenRows.length} sx={{ ml: 'auto' }} />}
            </Box>
            {tokenRows !== null && <Box component="ul" sx={{ listStyle: 'none', p: 0, m: 0, mt: 2, display: 'grid', gap: 1 }}>
              {tokenRows.length === 0 && <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>No tokens yet — generate the first one above.</Typography>}
              {tokenRows.map(row => (
                <Box component="li" key={row.name} sx={{ display: 'flex', alignItems: 'center', gap: 2, p: 1.5, border: 1, borderColor: 'divider', borderRadius: 1.5, flexWrap: 'wrap' }}>
                  <Box sx={{ minWidth: 0, flexGrow: 1 }}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }}>{row.name}{row.admin && <Chip size="small" label="admin" sx={{ ml: 1, height: 18, fontSize: 10 }} />}</Typography>
                    <Typography variant="caption" color="text.secondary">{ruleSummary(row.rules)}{formatCreated(row.created_at) ? ` · created ${formatCreated(row.created_at)}` : ''}</Typography>
                  </Box>
                  <Button
                    size="small"
                    color={confirmRevoke === row.name ? 'error' : undefined}
                    variant={confirmRevoke === row.name ? 'contained' : 'outlined'}
                    aria-label={`${confirmRevoke === row.name ? 'Confirm revoke' : 'Revoke'} token ${row.name}`}
                    className={confirmRevoke === row.name ? 'token-revoke armed' : 'token-revoke'}
                    onClick={() => (confirmRevoke === row.name ? revokeToken(row.name) : armRevoke(row.name))}
                  >
                    {confirmRevoke === row.name ? 'Confirm revoke' : 'Revoke'}
                  </Button>
                </Box>
              ))}
            </Box>}
          </Paper>
        </div>
      </section>
      <section className="writes-section" aria-labelledby="writes-heading">
        <div className="writes-layout">
          <Paper elevation={0} className="writes-panel write-proposals-region" sx={{ p: { xs: 2, md: 2.5 }, border: 1, borderColor: 'divider', display: 'grid', gap: 2 }}>
            <Box>
              <Typography variant="overline" color="text.secondary">WRITES</Typography>
              <Typography variant="h6" id="writes-heading">Write proposals</Typography>
              <Typography variant="body2" color="text.secondary">Review changes that need approval before they reach your vault.</Typography>
            </Box>
            <Box component="form" onSubmit={event => { event.preventDefault(); proposeFolder() }} sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, flexWrap: 'wrap', p: 1.5, border: 1, borderColor: 'divider', borderRadius: 1.5, bgcolor: 'background.default' }}>
              <TextField
                size="small"
                label="New folder path"
                value={newFolderPath}
                onChange={event => setNewFolderPath(event.target.value)}
                placeholder="AI/Inbox"
                inputProps={{ spellCheck: false }}
                sx={{ flex: '1 1 220px' }}
              />
              <Button type="submit" variant="contained" size="small" disabled={creatingFolder} aria-busy={creatingFolder} sx={{ mt: 0.5 }}>
                {creatingFolder ? 'Creating…' : 'Create folder'}
              </Button>
              {folderCreateError && <Alert severity="error" role="alert" sx={{ flexBasis: '100%', py: 0 }}>{folderCreateError}</Alert>}
            </Box>
            {writesStale && writes !== null && (
              <Box className="writes-stale" role="status" title={writesStale} sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', p: 1.5, border: 1, borderColor: 'warning.main', bgcolor: 'warning.light', borderRadius: 1.5 }}>
                <Typography variant="body2">live update failed — the list may be out of date</Typography>
                <Button size="small" onClick={() => loadWrites(true)}>retry</Button>
              </Box>
            )}
            <Box>
              <Typography variant="overline" color="text.secondary">PENDING</Typography>
              <Typography variant="h6">{pendingHeading}</Typography>
            </Box>
            {writesError && (
              <Box className="writes-error" role="alert" sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap', p: 1.5, border: 1, borderColor: 'error.main', bgcolor: 'error.light', borderRadius: 1.5 }}>
                <Typography variant="body2">{writesError}</Typography>
                <Button size="small" onClick={() => loadWrites()}>retry</Button>
              </Box>
            )}
            {!writesError && writes === null && (
              <Box className="writes-loading" role="status" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, color: 'text.secondary' }}>
                <CircularProgress size={16} /><Typography variant="body2">loading proposals…</Typography>
              </Box>
            )}
            {!writesError && writes !== null && pending.length === 0 && (
              <Typography variant="body2" color="text.secondary">no pending proposals — writes appear here when the index wants to add or update a note.</Typography>
            )}
            {pending.map(w => {
              const preview = w.operation === 'mkdir' ? 'Folder creation' : previewContent(w.content)
              return (
              <Box component="article" key={w.id} className="write-card" aria-label={`write proposal for ${w.path}`} sx={{ p: 2, border: 1, borderColor: 'divider', borderRadius: 2, bgcolor: 'background.default', display: 'grid', gap: 1.5 }}>
                <Box component="header" className="write-card-head" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
                  <Box component="code" className="write-path" sx={{ fontFamily: '"IBM Plex Mono", monospace', fontSize: 12 }}>{w.path}</Box>
                  <Box component="span" className="write-ops" sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
                    <Chip size="small" label={label(w.operation)} sx={{ height: 20, fontSize: 10 }} />
                    <Chip size="small" variant="outlined" label={label(w.rule_access)} sx={{ height: 20, fontSize: 10 }} />
                  </Box>
                </Box>
                <Box component="p" className="write-preview" title={preview} sx={{ fontFamily: '"IBM Plex Mono", monospace', fontSize: 12, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{preview}</Box>
                <Box component="footer" className="write-card-foot" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
                  <Box component="time" dateTime={w.requested_at} title={w.requested_at} sx={{ color: 'text.secondary', fontSize: 11, fontFamily: '"IBM Plex Mono", monospace' }}>{timeAgo(w.requested_at)} · {w.operation === 'mkdir' ? 'folder' : `${w.content.length.toLocaleString()} chars`}</Box>
                  <Box className="write-actions" sx={{ ml: 'auto', display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                    {actionErrors[w.id] ? <span className="write-action-error" role="alert" style={{ color: 'var(--mui-error-main)', fontSize: 12 }}>{actionErrors[w.id]}</span> : null}
                    <Button size="small" color="success" variant="contained" disabled={busy !== null} aria-label={`Approve ${w.path}`} onClick={() => resolveProposal('approve', w)}>
                      {busy?.id === w.id && busy.action === 'approve' ? 'approving…' : 'approve'}
                    </Button>
                    <Button size="small" color="error" variant="outlined" disabled={busy !== null} aria-label={`Reject ${w.path}`} onClick={() => resolveProposal('reject', w)}>
                      {busy?.id === w.id && busy.action === 'reject' ? 'rejecting…' : 'reject'}
                    </Button>
                  </Box>
                </Box>
              </Box>
              )
            })}
          </Paper>
          <section className="audit-region" aria-labelledby="audit-heading">
            <Box sx={{ mb: 2 }}>
              <Typography variant="overline" color="text.secondary">AUDIT</Typography>
              <Typography variant="h6" id="audit-heading">Recent activity</Typography>
            </Box>
            {writesError && (
              <Typography variant="body2" color="text.secondary">audit unavailable — the writes feed could not be loaded.</Typography>
            )}
            {!writesError && writes === null && (
              <Box role="status" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, color: 'text.secondary' }}>
                <CircularProgress size={16} /><Typography variant="body2">loading audit…</Typography>
              </Box>
            )}
            {!writesError && writes !== null && audit.length === 0 && (
              <Typography variant="body2" color="text.secondary">no writes yet — approved, rejected, and failed writes are audited here.</Typography>
            )}
            {!writesError && writes !== null && audit.length > 0 && (
              <Box component="ul" className="audit-list" sx={{ listStyle: 'none', p: 0, m: 0, display: 'grid', gap: 0.5 }}>
                {audit.slice(0, 10).map(w => (
                  <Box component="li" key={w.id} className={`audit-row audit-row-${w.status}`} sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap', p: 1.25, border: 1, borderColor: 'divider', borderRadius: 1.5 }}>
                    <Box component="code" className="audit-path" sx={{ fontFamily: '"IBM Plex Mono", monospace', fontSize: 12 }}>{w.path}</Box>
                    {w.operation === 'mkdir' && <Typography variant="caption" color="text.secondary">{w.status === 'applied' ? 'folder created' : 'folder proposal'}</Typography>}
                    <Chip size="small" label={statusLabel(w.status)} sx={{ height: 20, fontSize: 10, ml: 'auto' }} />
                    <Box component="time" className="audit-time" dateTime={w.resolved_at ?? w.requested_at} title={w.resolved_at ?? w.requested_at} sx={{ color: 'text.secondary', fontSize: 11, fontFamily: '"IBM Plex Mono", monospace' }}>{timeAgo(w.resolved_at ?? w.requested_at)}</Box>
                    {w.failure_reason && <Typography variant="caption" color="error.main" title={w.failure_reason}>{w.failure_reason}</Typography>}
                  </Box>
                ))}
              </Box>
            )}
          </section>
        </div>
        <span className="sr-only" role="status" aria-live="polite">{liveMessage}</span>
      </section>
      </div>
    </Box>
  )
}

/* Bootstrap only when mounted into a real page shell (skipped under jsdom tests). */
type RootHost = HTMLElement & { __neuralMemoryReactRoot?: Root }
const rootEl = document.getElementById('root') as RootHost | null
if (rootEl) {
  const root = rootEl.__neuralMemoryReactRoot ?? (rootEl.__neuralMemoryReactRoot = createRoot(rootEl))
  root.render(<React.StrictMode><App /></React.StrictMode>)
}
