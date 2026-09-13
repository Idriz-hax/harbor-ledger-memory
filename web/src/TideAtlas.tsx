import React, { useCallback, useEffect, useRef, useState } from 'react'
import cytoscape, { Core, StylesheetJson } from 'cytoscape'
import { Box, Button, Chip, Drawer, IconButton, Paper, Stack, Tooltip, Typography, useMediaQuery } from '@mui/material'
import { Add, CenterFocusStrong, Close, Fullscreen, Remove, Refresh, Shuffle, Terrain } from '@mui/icons-material'
import { api, type Activity } from './main'
import { activityColors, type ActivityKind } from './appTheme'
import { MAX_ZOOM, MIN_ZOOM, viewLevelForZoom, type GraphView, type GraphCluster } from './graphView'
import { buildCoastalPlacement, clearPositionOverrides, isIndexPath, loadPositionOverrides, savePositionOverride, settleOneHop, type Island } from './coastalLayout'
import { LiveTraversalController, type LiveTraversalEvent } from './liveTraversal'

type ViewState = { pan: { x: number; y: number }; zoom: number }

/* Map event types to activity colour categories. */
const eventToKind = (eventType: string): ActivityKind => {
  const t = eventType.toLowerCase()
  if (['read', 'query', 'retrieval', 'retrieve', 'get'].some(k => t.includes(k))) return 'read'
  if (['write', 'create', 'update', 'mutation', 'delete', 'insert'].some(k => t.includes(k))) return 'write'
  if (['scan', 'explore', 'index', 'reindex', 'crawl'].some(k => t.includes(k))) return 'scan'
  if (['propose', 'proposal', 'suggest', 'recommend'].some(k => t.includes(k))) return 'propose'
  return 'default'
}

const folderPalette = ['#59d9b1', '#6d9cff', '#bd7cff', '#ff8a70', '#e6bf69', '#45c2d9', '#e98fb4', '#a7d46f'] as const

export const topLevelFolder = (scope: string) => scope.split('/').filter(Boolean)[0] || '(root)'

const stableHash = (value: string) => [...value].reduce((hash, character) => ((hash * 31) + character.charCodeAt(0)) >>> 0, 2166136261)

/** Stable, high-contrast colors keep a folder recognizable across view changes. */
export const folderGroupColor = (folder: string) => folderPalette[stableHash(folder) % folderPalette.length]

export const readableActivityType = (eventType: string) => eventType
  .replace(/[._-]+/g, ' ')
  .trim()
  .replace(/\b\w/g, character => character.toUpperCase()) || 'Activity'

export const activityTarget = (event: Activity) => {
  const payload = event.payload as Record<string, unknown>
  const target = payload.path ?? payload.note_path ?? payload.scope ?? payload.target
  return typeof target === 'string' && target.trim() ? target : 'No target'
}

export const relativeActivityTime = (createdAt: string, now = Date.now()) => {
  const timestamp = Date.parse(createdAt)
  if (!Number.isFinite(timestamp)) return 'recently'
  const seconds = Math.max(0, Math.floor((now - timestamp) / 1000))
  if (seconds < 10) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

/* Build Cytoscape stylesheet — nautical dark theme with distinct node shapes. */
const computeStyles = (): StylesheetJson => [
  { selector: 'node', style: {
    label: 'data(label)',
    'background-color': 'data(color)',
    'border-color': 'data(color)',
    'border-width': '1.5',
    color: '#c8d8d6',
    'font-family': '"IBM Plex Sans", sans-serif',
    'font-size': '9',
    width: '14',
    height: '14',
    shape: 'ellipse',
    'text-valign': 'bottom',
    'text-margin-y': 5,
    'text-opacity': 0,
    'text-wrap': 'ellipsis',
    'text-max-width': '100',
  }},
  { selector: 'node.index-port', style: {
    shape: 'diamond',
    width: '20',
    height: '20',
    'border-width': '1.5',
  }},
  { selector: 'node.folder-hub', style: {
    shape: 'round-rectangle',
    width: '26',
    height: '26',
    'border-width': '2',
    'background-opacity': 0.6,
  }},
  { selector: 'node:selected', style: {
    'border-width': '3',
    'border-color': '#e6bf69',
    'text-opacity': 1,
    'font-weight': 'bold',
    'font-size': '11',
    'z-index': 99,
  }},
  { selector: 'node:active', style: {
    'border-color': '#e6bf69',
    'border-width': '2.5',
  }},
  { selector: 'edge', style: {
    'line-color': 'rgba(100, 140, 160, 0.2)',
    width: '1',
    opacity: 0.4,
    'curve-style': 'straight',
    'line-style': 'solid',
  }},
  { selector: 'edge:active', style: {
    'line-color': 'rgba(230, 191, 105, 0.4)',
    opacity: 0.6,
    width: '1.5',
  }},
  { selector: '.is-muted', style: {
    opacity: 0.16,
  }},
  { selector: 'node.is-neighbor', style: {
    'text-opacity': 1,
    'z-index': 98,
  }},
  { selector: 'edge.is-neighbor', style: {
    'line-color': 'rgba(89, 217, 177, .65)',
    opacity: 0.9,
    width: '1.5',
  }},
  { selector: 'node.traversal-read', style: ({ 'border-color': '#62e8f2', 'border-width': '3.5', 'shadow-color': '#62e8f2', 'shadow-blur': 16, 'shadow-opacity': 0.72, 'shadow-offset-x': 0, 'shadow-offset-y': 0, 'z-index': 125 } as any) },
  { selector: 'node.traversal-write', style: ({ 'border-color': '#f4bd62', 'border-width': '3.5', 'shadow-color': '#f4bd62', 'shadow-blur': 16, 'shadow-opacity': 0.72, 'shadow-offset-x': 0, 'shadow-offset-y': 0, 'z-index': 125 } as any) },
  { selector: 'edge.traversal-forward', style: {
    'line-color': '#62e8f2', 'target-arrow-color': '#62e8f2', 'target-arrow-shape': 'triangle', 'arrow-scale': 1.2,
    opacity: 1, width: '3', 'line-style': 'dashed', 'line-dash-pattern': [8, 5], 'line-dash-offset': 0, 'z-index': 124,
  }},
  { selector: 'edge.traversal-forward-write', style: {
    'line-color': '#f4bd62', 'target-arrow-color': '#f4bd62', 'target-arrow-shape': 'triangle', 'arrow-scale': 1.2,
    opacity: 1, width: '3', 'line-style': 'dashed', 'line-dash-pattern': [8, 5], 'line-dash-offset': 0, 'z-index': 124,
  }},
  { selector: 'node.activity-glow', style: {
    'border-color': '#f5d889',
    'border-width': '4',
    'z-index': 120,
  }},
  { selector: 'node.route-trace', style: {
    'border-width': '2.5',
    'z-index': 110,
  }},
  { selector: 'edge.route-trace', style: {
    'line-color': '#59d9b1',
    opacity: 1,
    width: '2.5',
    'line-style': 'dashed',
    'line-dash-pattern': [8, 5],
  }},
  { selector: 'node.activity-beacon', style: {
    'border-color': '#e6bf69',
    'border-width': '3',
    'z-index': 115,
  }},
]

/* Diff the bounded view onto the existing core. */
const applyView = (instance: Core, view: GraphView, layoutSeed?: string | number) => {
  const generation = view.generation ?? `${view.level}:${view.scope ?? 'vault'}`
  const placement = buildCoastalPlacement(
    view.clusters.map(cluster => ({ id: cluster.id, scope: cluster.scope, kind: cluster.kind })),
    view.edges,
    loadPositionOverrides(generation),
    layoutSeed,
  )
  const desiredNodes = view.clusters.map((cluster) => ({
    data: {
      id: cluster.id,
      label: cluster.label,
      kind: cluster.kind,
      count: cluster.member_count,
      scope: cluster.scope,
       color: folderGroupColor(topLevelFolder(cluster.scope)),
       folder: topLevelFolder(cluster.scope),
       island: topLevelFolder(cluster.scope),
    },
    position: placement.positions[cluster.id],
    locked: false,
       classes: view.level === 2 && isIndexPath(cluster.scope) ? 'index-port'
      : 'file-node',
  }))
  const desiredEdges = view.edges.map(edge => ({ data: { id: edge.id, source: edge.source, target: edge.target, weight: edge.weight, edge_type: edge.edge_type } }))
  const desiredIds = new Set([...desiredNodes.map(n => String(n.data.id)), ...desiredEdges.map(e => String(e.data.id))])
  const current = instance.elements()
  const currentIds = new Set(current.map(el => String(el.id())))
  current.filter(el => !desiredIds.has(String(el.id()))).remove()

  /* Update colours on persisting nodes. */
  const existingNodes = instance.nodes().map(en => ({ id: String(en.data('id')), color: String(en.data('color')) }))
  desiredNodes.forEach(n => {
    const existing = existingNodes.find(en => en.id === String(n.data.id))
    if (existing && existing.color !== String(n.data.color)) {
      instance.getElementById(String(n.data.id)).forEach(el => { el.data('color', n.data.color) })
    }
  })

  const additions = [
    ...desiredNodes.filter(n => !currentIds.has(String(n.data.id))),
    ...desiredEdges.filter(e => !currentIds.has(String(e.data.id))),
  ]
  if (additions.length) instance.add(additions)
  instance.nodes().forEach(node => {
    const position = placement.positions[node.id()]
    if (position) node.position(position)
  })
  return { placement, generation }
}

const coastHash = (value: string) => [...value].reduce((acc, char) => ((acc * 31) + char.charCodeAt(0)) >>> 0, 2166136261)
export const nodeMotionPhase = (id: string) => {
  const hash = coastHash(`tide-motion:${id}`)
  return {
    x: (hash % 628) / 100,
    y: ((hash >>> 8) % 628) / 100,
    periodX: 20 + (hash % 16),
    periodY: 20 + ((hash >>> 12) % 16),
  }
}

export const idleFloatOffset = (id: string, seconds: number) => {
  const phase = nodeMotionPhase(id)
  return {
    x: Math.sin((seconds / phase.periodX) * Math.PI * 2 + phase.x) * 0.75,
    y: Math.sin((seconds / phase.periodY) * Math.PI * 2 + phase.y) * 0.75,
  }
}

/* A deterministic, bounded coastline: fixed segments keep the overlay calm and cheap. */
const coastPath = (radius: number, key: string) => {
  const phase = (coastHash(key) % 628) / 100
  const segments = 24
  const points = Array.from({ length: segments }, (_, index) => {
    const angle = (index / segments) * Math.PI * 2
    const wobble = 1 + Math.sin(angle * 3 + phase) * 0.08 + Math.sin(angle * 5 + phase * 0.7) * 0.035
    return `${Math.cos(angle) * radius * wobble} ${Math.sin(angle) * radius * wobble}`
  })
  return points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point}`).join(' ') + ' Z'
}

/* Re-anchor the visual island to the positions that the final layout actually produced. */
const islandsFromFinalPositions = (instance: Core, islands: Island[]): Island[] => islands.map(island => {
  const points: { x: number; y: number }[] = []
  instance.nodes().filter(node => island.nodeIds.includes(node.id())).forEach(node => { points.push(node.position()) })
  if (!points.length) return island
  const center = {
    x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
    y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
  }
  const radius = Math.max(island.radius, ...points.map(point => Math.hypot(point.x - center.x, point.y - center.y) + 34))
  return { ...island, center, radius }
})

export function TideAtlas({ events, onRefresh }: { events: Activity[]; onRefresh?: () => void }) {
  const container = useRef<HTMLDivElement>(null)
  const chartContainer = useRef<HTMLDivElement>(null)
  const coreRef = useRef<Core | null>(null)
  const topoRef = useRef<SVGSVGElement>(null)
  const [view, setView] = useState<GraphView | null>(null)
  const [selected, setSelected] = useState<GraphCluster | null>(null)
  const [viewport, setViewport] = useState<ViewState>({ pan: { x: 0, y: 0 }, zoom: 0.8 })
  const [scope, setScope] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [fullScreen, setFullScreen] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [traversalConnected, setTraversalConnected] = useState(false)
  const [activeTraversalCount, setActiveTraversalCount] = useState(0)
  const [islands, setIslands] = useState<Island[]>([])
  const [layoutRevision, setLayoutRevision] = useState(0)
  const viewRef = useRef(view)
  const scopeRef = useRef(scope)
  const viewportRef = useRef(viewport)
  const requestGenerationRef = useRef(0)
  const rearrangeNonceRef = useRef(0)
  const requestControllerRef = useRef<AbortController | null>(null)
  const traversalControllerRef = useRef<LiveTraversalController | null>(null)
  const levelDebounceRef = useRef<number | null>(null)
  const initialWholeVaultLayoutRef = useRef(false)
  const suppressViewportHandlingRef = useRef(false)
  const pulseRef = useRef<{ cancelled: boolean; elements: ReturnType<Core['elements']> | null; timer: number | null } | null>(null)
  const draggingRef = useRef(new Set<string>())
  const restingPositionsRef = useRef<Record<string, { x: number; y: number }>>({})
  const dragSamplesRef = useRef(new Map<string, { x: number; y: number; time: number }[]>())
  const momentumFramesRef = useRef(new Map<string, number>())
  const lastEventId = useRef<number | null>(null)
  viewRef.current = view
  scopeRef.current = scope
  viewportRef.current = viewport
  const small = useMediaQuery('(max-width:700px)')
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const reducedMotionRef = useRef(reducedMotion)
  reducedMotionRef.current = reducedMotion

  const resizeChart = useCallback(() => {
    coreRef.current?.resize()
  }, [])

  useEffect(() => {
    const chart = chartContainer.current
    if (!chart || typeof document === 'undefined') return
    const onFullscreenChange = () => {
      setFullScreen(document.fullscreenElement === chart)
      window.requestAnimationFrame(resizeChart)
    }
    document.addEventListener('fullscreenchange', onFullscreenChange)
    return () => document.removeEventListener('fullscreenchange', onFullscreenChange)
  }, [resizeChart])

  const loadView = useCallback(async (level: number, nextScope: string | null = scopeRef.current) => {
    const generation = ++requestGenerationRef.current
    requestControllerRef.current?.abort()
    const controller = new AbortController()
    requestControllerRef.current = controller
    try {
      setError('')
      let cursor: string | null = null
      let firstResponse: GraphView | null = null
      const clusters = new Map<string, GraphCluster>()
      const edges = new Map<string, GraphView['edges'][number]>()
      do {
        const params = new URLSearchParams({ level: String(level), page_size: '500' })
        if (nextScope) params.set('scope', nextScope)
        if (cursor) params.set('cursor', cursor)
        const response = await api<GraphView & { next_cursor?: string | null }>(`/api/v1/graph/view?${params}`, { signal: controller.signal })
        if (!firstResponse) firstResponse = response
        response.clusters?.forEach(cluster => clusters.set(cluster.id, cluster))
        response.edges?.forEach(edge => edges.set(edge.id, edge))
        cursor = response.next_cursor ?? null
      } while (cursor)
      if (generation === requestGenerationRef.current && firstResponse && Array.isArray(firstResponse.clusters) && Array.isArray(firstResponse.edges)) {
        setView({ ...firstResponse, clusters: [...clusters.values()], edges: [...edges.values()] })
      }
    } catch (e) {
      if (generation === requestGenerationRef.current && nextScope && e instanceof Error && e.message === 'scope is not accessible or contains no notes') {
        scopeRef.current = null
        setScope(null)
        setSelected(null)
        void loadView(2, null)
        return
      }
      if (generation === requestGenerationRef.current && !(e instanceof DOMException && e.name === 'AbortError')) {
        setError(e instanceof Error ? e.message : 'chart unavailable')
      }
    }
  }, [])

  /* Default: load all individual files (level 2, no scope = whole vault). */
  useEffect(() => { loadView(2, null) }, [loadView])

  const syncViewport = useCallback(() => {
    const instance = coreRef.current
    if (!instance) return
    const pan = typeof instance.pan === 'function' ? instance.pan() : { x: 0, y: 0 }
     const zoom = typeof instance.zoom === 'function' ? instance.zoom() : 0.8
     const next = { pan: pan as { x: number; y: number }, zoom: zoom as number }
     viewportRef.current = next
     setViewport(next)
     if (suppressViewportHandlingRef.current) {
       if (levelDebounceRef.current !== null) window.clearTimeout(levelDebounceRef.current)
       levelDebounceRef.current = null
       return
     }
     /* Debounced level-change check: only fires if zoom is stable for 400ms. */
    if (levelDebounceRef.current !== null) window.clearTimeout(levelDebounceRef.current)
    levelDebounceRef.current = window.setTimeout(() => {
      const currentView = viewRef.current
      const currentZoom = viewportRef.current.zoom
      if (scopeRef.current && currentView?.level === 2) return
      const level = viewLevelForZoom(currentZoom, Boolean(scopeRef.current), currentView?.level)
      if (currentView && level !== currentView.level) {
        loadView(level, scopeRef.current)
      }
    }, 400)
  }, [loadView])

  /* Create the core exactly once. */
  useEffect(() => {
    if (!container.current || coreRef.current) return
    const instance = cytoscape({
      container: container.current,
      elements: [],
      layout: { name: 'null' },
      minZoom: MIN_ZOOM,
      maxZoom: MAX_ZOOM,
      style: computeStyles(),
    })
    coreRef.current = instance
    const traversalController = new LiveTraversalController(instance, {
      reducedMotion,
      onActivityChange: setActiveTraversalCount,
      nodeIdForPath: path => {
        const current = viewRef.current
        if (!current) return undefined
        return current.clusters
          .filter(cluster => path === cluster.scope || path.startsWith(`${cluster.scope}/`))
          .sort((left, right) => right.scope.length - left.scope.length)[0]?.id
      },
      edgeIdForEvent: event => {
        const current = viewRef.current
        if (!current || !event.source_path || !event.target_path || !event.edge_type) return undefined
        const resolve = (path: string) => {
          return current.clusters
            .filter(cluster => path === cluster.scope || path.startsWith(`${cluster.scope}/`))
            .sort((left, right) => right.scope.length - left.scope.length)[0]?.id
        }
        const source = resolve(event.source_path)
        const target = resolve(event.target_path)
        if (!source || !target) return undefined
        const matches = current.edges.filter(edge => edge.source === source && edge.target === target && edge.edge_type === event.edge_type)
        return matches.length === 1 ? matches[0].id : undefined
      },
    })
    traversalControllerRef.current = traversalController
    instance.on('tap', 'node', event => {
      const id = event.target.id()
      const selectedNode = instance.getElementById(id)
      const connectedIds = new Set([id])
      instance.edges().forEach(edge => {
        const source = String(edge.data('source'))
        const target = String(edge.data('target'))
        if (source === id || target === id) {
          connectedIds.add(source)
          connectedIds.add(target)
        }
      })
      const allElements = instance.elements()
      allElements.removeClass('is-muted is-neighbor')
      allElements.addClass('is-muted')
      const neighborhood = instance.elements().filter(element => {
        const elementId = element.id()
        return connectedIds.has(elementId) || (element.data('source') === id) || (element.data('target') === id)
      })
      neighborhood.removeClass('is-muted')
      neighborhood.addClass('is-neighbor')
      selectedNode.removeClass('is-muted')
      selectedNode.addClass('is-neighbor')
      setSelected(viewRef.current?.clusters.find(cluster => cluster.id === id) ?? null)
    })
    instance.on('tap', event => {
      if (event.target === instance) {
        instance.elements().removeClass('is-muted is-neighbor')
        setSelected(null)
      }
    })
     instance.on('pan zoom', syncViewport)
    instance.on('grab', 'node', event => {
      const node = event.target
      const activeFrame = momentumFramesRef.current.get(node.id())
      if (activeFrame !== undefined) window.cancelAnimationFrame(activeFrame)
      momentumFramesRef.current.delete(node.id())
      draggingRef.current.add(node.id())
      dragSamplesRef.current.set(node.id(), [{ ...node.position(), time: performance.now() }])
    })
    instance.on('drag position', 'node', event => {
      const node = event.target
      if (!draggingRef.current.has(node.id())) return
      const samples = dragSamplesRef.current.get(node.id()) ?? []
      samples.push({ ...node.position(), time: performance.now() })
      dragSamplesRef.current.set(node.id(), samples.slice(-4))
    })
    instance.on('dragfree', 'node', event => {
      const node = event.target
      const currentView = viewRef.current
      if (!currentView) return
      const generation = currentView.generation ?? `${currentView.level}:${currentView.scope ?? 'vault'}`
      draggingRef.current.delete(node.id())
      const samples = dragSamplesRef.current.get(node.id()) ?? []
      dragSamplesRef.current.delete(node.id())
      const settled = settleOneHop(
        Object.fromEntries(instance.nodes().map(candidate => [candidate.id(), candidate.position()])),
        currentView.edges,
        node.id(),
      )
      Object.entries(settled).forEach(([id, position]) => {
        instance.nodes().filter(candidate => candidate.id() === id).forEach(candidate => { candidate.position(position) })
      })
      restingPositionsRef.current = { ...restingPositionsRef.current, ...settled }
      Object.entries(settled).forEach(([id, position]) => savePositionOverride(generation, id, position))
      if (reducedMotionRef.current || samples.length < 2) return
      const first = samples[0]
      const last = samples[samples.length - 1]
      const dt = Math.max(16, last.time - first.time)
      const velocity = { x: (last.x - first.x) / dt, y: (last.y - first.y) / dt }
      const started = performance.now()
      const frame = (time: number) => {
        const elapsed = time - started
        const progress = Math.min(1, elapsed / 280)
        const damping = Math.pow(1 - progress, 2)
        const current = node.position()
        const next = { x: current.x + velocity.x * 16 * damping, y: current.y + velocity.y * 16 * damping }
        node.position(next)
        const affected = settleOneHop(Object.fromEntries(instance.nodes().map(candidate => [candidate.id(), candidate.position()])), currentView.edges, node.id(), 18)
        Object.entries(affected).forEach(([id, position]) => instance.getElementById(id).forEach(candidate => { candidate.position(position) }))
        restingPositionsRef.current = { ...restingPositionsRef.current, ...affected }
        Object.entries(affected).forEach(([id, position]) => savePositionOverride(generation, id, position))
        if (progress < 1) momentumFramesRef.current.set(node.id(), window.requestAnimationFrame(frame))
        else momentumFramesRef.current.delete(node.id())
      }
      momentumFramesRef.current.set(node.id(), window.requestAnimationFrame(frame))
    })
    syncViewport()
    return () => {
      if (levelDebounceRef.current !== null) window.clearTimeout(levelDebounceRef.current)
      requestControllerRef.current?.abort()
      traversalController.dispose()
      traversalControllerRef.current = null
      momentumFramesRef.current.forEach(frame => window.cancelAnimationFrame(frame))
      momentumFramesRef.current.clear()
      instance.destroy()
      coreRef.current = null
    }
  }, [syncViewport])

  useEffect(() => { traversalControllerRef.current?.setReducedMotion(reducedMotion) }, [reducedMotion])

  /* Traversal is deliberately separate from the activity history stream. */
  useEffect(() => {
    if (typeof EventSource === 'undefined') return
    const stream = new EventSource('/api/v1/graph/traversal/stream')
    stream.onopen = () => setTraversalConnected(true)
    stream.onerror = () => setTraversalConnected(false)
    const onTraversal = (event: Event) => {
      try { traversalControllerRef.current?.apply(JSON.parse((event as MessageEvent).data) as LiveTraversalEvent) } catch { /* ignore malformed events */ }
    }
    stream.addEventListener('traversal', onTraversal)
    return () => { stream.removeEventListener('traversal', onTraversal); stream.close(); setTraversalConnected(false) }
  }, [])

  /* Apply view data without a global layout. Every node stays draggable. */
  useEffect(() => {
    const instance = coreRef.current
    if (!instance || !view) return
    const { placement, generation } = applyView(instance, view)
    traversalControllerRef.current?.flush()
    if (!initialWholeVaultLayoutRef.current) {
      /* The first view is the only automatic viewport adjustment. Cytoscape's
         fit emits pan/zoom events, so keep those events out of LOD handling. */
      suppressViewportHandlingRef.current = true
       instance.fit(instance.nodes(), 72)
       syncViewport()
      suppressViewportHandlingRef.current = false
    }
    setIslands(placement.islands)
    restingPositionsRef.current = { ...restingPositionsRef.current, ...placement.positions }
    initialWholeVaultLayoutRef.current = true
  }, [view])

  /* Trace only the target and its directly connected route. No layout or viewport
     work happens here: activity is a visual signal layered over stable positions. */
  useEffect(() => {
    const instance = coreRef.current
    if (!instance || !events.length) return
    const latest = events[0]
    if (lastEventId.current === latest.id) return
    lastEventId.current = latest.id
    const payload = latest.payload as Record<string, string>
    const targetPath = payload.path ?? payload.note_path ?? payload.scope ?? ''
    if (!targetPath) return
    const matches = instance.nodes().filter(n => {
      const nodeId = String(n.data('id') ?? '')
      const nodeScope = String(n.data('scope') ?? '')
      return nodeId === targetPath || nodeScope === targetPath || targetPath.includes(nodeId)
    })
    if (!matches.length) return
    const targetIds = new Set(matches.map(node => node.id()))
    const routeEdges = instance.edges().filter(edge => targetIds.has(String(edge.data('source'))) || targetIds.has(String(edge.data('target'))))
    const routeNodeIds = new Set([...targetIds, ...routeEdges.map(edge => String(edge.data('source'))), ...routeEdges.map(edge => String(edge.data('target')))])
    const routeNodes = instance.nodes().filter(node => routeNodeIds.has(node.id()))
    const prev = pulseRef.current
    if (prev) { prev.cancelled = true; if (prev.timer !== null) window.clearTimeout(prev.timer) }
    const handle = { cancelled: false, elements: routeEdges, timer: null as number | null }
    pulseRef.current = handle
    const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const duration = reduced ? 0 : 260
    matches.addClass('activity-glow')
    routeNodes.addClass('route-trace')
    routeEdges.addClass('route-trace')
    const kind = eventToKind(latest.event_type)
    if (kind === 'write' || kind === 'scan') matches.addClass('activity-beacon')
    routeNodes.animate({ style: { opacity: 1 } as Record<string, unknown>, duration })
    routeEdges.animate({ style: { opacity: 1 } as Record<string, unknown>, duration })
    if (kind === 'write' || kind === 'scan') {
      matches.animate({ style: { 'border-width': 6, opacity: 0.65 } as Record<string, unknown>, duration })
    }
    const midTimer = window.setTimeout(() => {
      if (handle.cancelled) return
      routeNodes.animate({ style: { opacity: 1 } as Record<string, unknown>, duration })
      routeEdges.animate({ style: { opacity: 0.4 } as Record<string, unknown>, duration })
      if (kind === 'write' || kind === 'scan') matches.animate({ style: { 'border-width': 2, opacity: 1 } as Record<string, unknown>, duration })
    }, duration)
    handle.timer = window.setTimeout(() => {
      if (handle.cancelled) return
      window.clearTimeout(midTimer)
       routeEdges.stop()
       matches.removeClass('activity-glow activity-beacon')
       routeNodes.removeClass('route-trace')
       routeEdges.removeClass('route-trace')
       if (pulseRef.current === handle) pulseRef.current = null
    }, reduced ? 0 : 900)
  }, [events])

  /* Cleanup pulse on unmount. */
  useEffect(() => () => {
    const prev = pulseRef.current
    if (prev) { prev.cancelled = true; if (prev.timer !== null) window.clearTimeout(prev.timer); prev.elements?.stop() }
  }, [])

  const zoomBy = (factor: number) => {
    const instance = coreRef.current
    if (!instance) return
    const currentZoom = typeof instance.zoom === 'function' ? instance.zoom() : viewportRef.current.zoom
    instance.zoom(Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, currentZoom * factor)))
  }
  const selectCluster = async (cluster: GraphCluster) => { scopeRef.current = cluster.scope; setScope(cluster.scope); setSelected(cluster); await loadView(2, cluster.scope) }
  const handleRescan = async () => {
    if (scanning) return
    setScanning(true); setError('')
    try {
      await api('/api/v1/scan', { method: 'POST' })
      onRefresh?.()
      await loadView(viewRef.current?.level ?? 2, scopeRef.current)
    } catch (e) { setError(e instanceof Error ? e.message : 'rescan failed') }
    finally { setScanning(false) }
  }
  const chartGeneration = (currentView: GraphView) => currentView.generation ?? `${currentView.level}:${currentView.scope ?? 'vault'}`
  const rearrangeChart = () => {
    const instance = coreRef.current
    const currentView = viewRef.current
    if (!instance || !currentView) return
    const generation = chartGeneration(currentView)
    /* Rearrange is a local reset with a fresh seed. Clear drag overrides first
       so the freshly computed placement is also what the next chart refresh uses. */
    clearPositionOverrides(generation)
    rearrangeNonceRef.current += 1
    const { placement } = applyView(instance, currentView, `rearrange-${rearrangeNonceRef.current}`)
    instance.nodes().forEach(node => {
      const position = placement.positions[node.id()]
      if (position) node.position(position)
    })
    restingPositionsRef.current = { ...restingPositionsRef.current, ...placement.positions }
    setIslands(islandsFromFinalPositions(instance, placement.islands))
    setLayoutRevision(revision => revision + 1)
  }
  const resetChartLayout = () => {
    const instance = coreRef.current
    const currentView = viewRef.current
    if (!instance || !currentView) return
    const generation = chartGeneration(currentView)
    clearPositionOverrides(generation)
    const { placement } = applyView(instance, currentView)
    instance.nodes().forEach(node => {
      const position = placement.positions[node.id()]
      if (position) node.position(position)
    })
    setIslands(placement.islands)
    suppressViewportHandlingRef.current = true
    instance.fit(instance.nodes(), 72)
    syncViewport()
    suppressViewportHandlingRef.current = false
  }
  const toggleFullscreen = async () => {
    const chart = chartContainer.current
    if (!chart || typeof document === 'undefined') return
    try {
      if (document.fullscreenElement === chart) {
        if (typeof document.exitFullscreen === 'function') await document.exitFullscreen()
      } else if (typeof chart.requestFullscreen === 'function') {
        await chart.requestFullscreen()
      }
    } catch {
      /* Fullscreen is optional; browsers can reject it without affecting the chart. */
    }
  }
  const chartHeight = fullScreen ? '100vh' : small ? 'calc(100vh - 180px)' : 'min(760px, calc(100vh - 250px))'

  /* Last 5 events for the compact radar panel. */
  const radarEvents = events.slice(0, 5)
  const folderGroups = [...new Set((view?.clusters ?? []).map(cluster => topLevelFolder(cluster.scope)))].sort()

  const toScreenX = (x: number) => x * viewport.zoom + viewport.pan.x
  const toScreenY = (y: number) => y * viewport.zoom + viewport.pan.y

  return <Box sx={{ width: '100%', maxWidth: 'none', mx: 'auto' }}>
    <Stack direction={{ xs: 'column', md: 'row' }} justifyContent="space-between" alignItems={{ md: 'end' }} gap={2} mb={2}>
      <Box>
        <Typography variant="overline" color="primary">MEMORY CHART · TIDE ATLAS</Typography>
        <Typography variant="h2" sx={{ fontSize: { xs: '2.2rem', md: '3.2rem' } }}>{view ? `${view.clusters.reduce((sum, c) => sum + c.member_count, 0)} notes charted` : 'Charting…'}</Typography>
        <Typography color="text.secondary">A bounded survey of the local ledger. Zoom in to enter an archive; zoom out to read its coastline.</Typography>
      </Box>
      <Stack direction="row" gap={1} flexWrap="wrap" alignItems="center">
        <Box className="live-indicator">
          <Box className={!traversalConnected ? 'live-dot disconnected' : 'live-dot'} />
          <span>{traversalConnected ? `LIVE • ${activeTraversalCount} active` : 'RECONNECTING'}</span>
        </Box>
        <Chip icon={<Terrain />} label={scanning ? 'scanning' : 'active'} color="primary" variant="outlined" sx={{ borderColor: 'rgba(118, 163, 174, .3)', color: 'text.secondary', fontSize: 11, fontFamily: '"IBM Plex Mono", monospace' }} />
        <Button startIcon={<Refresh />} onClick={handleRescan} disabled={scanning} sx={{ color: 'text.secondary', border: 1, borderColor: 'rgba(118, 163, 174, .3)', borderRadius: 6, '&:hover': { borderColor: 'rgba(230, 191, 105, .5)', background: 'rgba(230, 191, 105, .06)' } }}>
          {scanning ? 'Scanning…' : 'Rescan'}
        </Button>
      </Stack>
    </Stack>
    {error && <Box sx={{ mb: 1, p: 1.5, borderRadius: 6, background: 'rgba(240, 128, 114, .1)', border: '1px solid rgba(240, 128, 114, .3)', color: '#f08072', fontSize: 13, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}><span>{error}</span><Button size="small" onClick={() => loadView(view?.level ?? 2, scope)} sx={{ color: '#f08072', fontSize: 12 }}>Retry</Button></Box>}
     <Paper ref={chartContainer} className="tide-atlas" sx={{ position: fullScreen ? 'fixed' : 'relative', inset: fullScreen ? 0 : undefined, zIndex: fullScreen ? 1200 : undefined, height: chartHeight, overflow: 'hidden', bgcolor: 'transparent', border: '1px solid rgba(118, 163, 174, .15)' }}>
      {/* Cytoscape graph */}
      <Box ref={container} role="application" tabIndex={0} aria-label="Tide Atlas memory chart. Pan and zoom to explore notes." sx={{ position: 'absolute', inset: 0, cursor: 'grab', '&:active': { cursor: 'grabbing' } }} />

      {/* Controls — top-left */}
       <Stack direction="row" spacing={.5} sx={{ position: 'absolute', top: 14, left: 14, zIndex: 3 }} data-layout-revision={layoutRevision}>
          <Tooltip title="Zoom in"><IconButton aria-label="Zoom in" onClick={() => zoomBy(1.25)} sx={{ color: 'rgba(233, 243, 242, .7)', border: 1, borderColor: 'rgba(118, 163, 174, .25)', borderRadius: 1, background: 'rgba(10, 16, 22, .6)', backdropFilter: 'blur(6px)', '&:hover': { borderColor: 'rgba(230, 191, 105, .4)', background: 'rgba(230, 191, 105, .08)' } }}><Add sx={{ fontSize: 16 }} /></IconButton></Tooltip>
          <Tooltip title="Zoom out"><IconButton aria-label="Zoom out" onClick={() => zoomBy(.8)} sx={{ color: 'rgba(233, 243, 242, .7)', border: 1, borderColor: 'rgba(118, 163, 174, .25)', borderRadius: 1, background: 'rgba(10, 16, 22, .6)', backdropFilter: 'blur(6px)', '&:hover': { borderColor: 'rgba(230, 191, 105, .4)', background: 'rgba(230, 191, 105, .08)' } }}><Remove sx={{ fontSize: 16 }} /></IconButton></Tooltip>
          <Tooltip title="Rearrange layout"><IconButton aria-label="Rearrange layout" onClick={rearrangeChart} sx={{ color: 'rgba(233, 243, 242, .7)', border: 1, borderColor: 'rgba(118, 163, 174, .25)', borderRadius: 1, background: 'rgba(10, 16, 22, .6)', backdropFilter: 'blur(6px)', '&:hover': { borderColor: 'rgba(230, 191, 105, .4)', background: 'rgba(230, 191, 105, .08)' } }}><Shuffle sx={{ fontSize: 16 }} /></IconButton></Tooltip>
          <Tooltip title="Reset view"><IconButton aria-label="Reset view" onClick={resetChartLayout} sx={{ color: 'rgba(233, 243, 242, .7)', border: 1, borderColor: 'rgba(118, 163, 174, .25)', borderRadius: 1, background: 'rgba(10, 16, 22, .6)', backdropFilter: 'blur(6px)', '&:hover': { borderColor: 'rgba(230, 191, 105, .4)', background: 'rgba(230, 191, 105, .08)' } }}><CenterFocusStrong sx={{ fontSize: 16 }} /></IconButton></Tooltip>
          <Tooltip title={fullScreen ? 'Exit fullscreen' : 'Enter fullscreen'}><IconButton aria-label={fullScreen ? 'Exit fullscreen' : 'Enter fullscreen'} onClick={toggleFullscreen} sx={{ color: 'rgba(233, 243, 242, .7)', border: 1, borderColor: 'rgba(118, 163, 174, .25)', borderRadius: 1, background: 'rgba(10, 16, 22, .6)', backdropFilter: 'blur(6px)', '&:hover': { borderColor: 'rgba(230, 191, 105, .4)', background: 'rgba(230, 191, 105, .08)' } }}>{fullScreen ? <Close sx={{ fontSize: 16 }} /> : <Fullscreen sx={{ fontSize: 16 }} />}</IconButton></Tooltip>
      </Stack>

      {/* Compass / level indicator — top-right */}
      <Box sx={{ position: 'absolute', right: 16, top: 16, zIndex: 3, color: 'rgba(233, 243, 242, .5)', fontFamily: '"IBM Plex Mono", monospace', fontSize: 10, letterSpacing: '.1em' }}>N · TIDE ATLAS · {view?.level === 2 ? 'FILES' : 'COAST'}</Box>

      {/* Zoom / bearing count — bottom-center */}
      <Box sx={{ position: 'absolute', bottom: 14, left: '50%', transform: 'translateX(-50%)', zIndex: 3, color: 'rgba(233, 243, 242, .4)', fontFamily: '"IBM Plex Mono", monospace', fontSize: 10 }}>
        zoom {viewport.zoom.toFixed(2)} · {view?.clusters.length ?? 0} bearings
      </Box>

      {/* Legend — bottom-left */}
       <Box className="atlas-legend glass-panel">
         {folderGroups.length > 0 && <>
           <Box className="legend-heading">FOLDERS</Box>
           {folderGroups.map(folder => <Box className="legend-item" key={folder}>
             <Box className="legend-dot" sx={{ background: folderGroupColor(folder), boxShadow: `0 0 5px ${folderGroupColor(folder)}` }} />
             <span>{folder}</span>
           </Box>)}
           <Box sx={{ borderTop: '1px solid rgba(118,163,174,.15)', mt: .5, pt: .5 }} />
         </>}
         <Box className="legend-item"><Box className="legend-dot read" /><span>read</span></Box>
        <Box className="legend-item"><Box className="legend-dot write" /><span>write</span></Box>
        <Box className="legend-item"><Box className="legend-dot scan" /><span>scan</span></Box>
        <Box className="legend-item"><Box className="legend-dot propose" /><span>propose</span></Box>
        <Box className="legend-item"><Box className="legend-dot memory" /><span>memory</span></Box>
        <Box className="legend-item" sx={{ mt: .5, pt: .5, borderTop: '1px solid rgba(118,163,174,.15)' }}><Box sx={{ width: 10, height: 10, borderRadius: '50%', background: 'rgba(84, 110, 122, .6)', border: '1px solid rgba(84, 110, 122, .8)' }} /><span>file</span></Box>
         <Box className="legend-item"><Box sx={{ width: 10, height: 10, transform: 'rotate(45deg)', background: 'rgba(200, 216, 214, .34)', border: '1px solid rgba(200, 216, 214, .72)' }} /><span>index</span></Box>
      </Box>

       {/* Activity radar — bottom-right */}
       <Box className="activity-radar glass-panel" aria-label="Recent activity">
         <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: .75 }}>
           <Box className="radar-title">ACTIVITY</Box>
           <Typography component="span" sx={{ fontSize: 10, color: 'rgba(233, 243, 242, .45)', fontFamily: '"IBM Plex Mono", monospace' }}>
             {events.length} {events.length === 1 ? 'event' : 'events'}
           </Typography>
         </Stack>
         {radarEvents.length === 0 ? (
           <Typography component="p" sx={{ m: 0, fontSize: 11, color: 'rgba(233, 243, 242, .5)' }}>No recent activity</Typography>
         ) : radarEvents.map(event => {
           const kind = eventToKind(event.event_type)
           const target = activityTarget(event)
           const shortTarget = target.length > 28 ? `…${target.slice(-27)}` : target
           return (
             <Box key={event.id} className="radar-event" title={`${readableActivityType(event.event_type)} · ${target}`}>
               <Box className="radar-dot" sx={{ background: activityColors[kind], boxShadow: `0 0 5px ${activityColors[kind]}` }} />
               <Box sx={{ minWidth: 0, lineHeight: 1.15 }}>
                 <Box component="span" sx={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{readableActivityType(event.event_type)}</Box>
                 <Box component="span" sx={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'rgba(233, 243, 242, .48)', fontSize: 10 }}>{shortTarget}</Box>
               </Box>
               <Box component="span" sx={{ ml: 'auto', pl: .75, flexShrink: 0, color: 'rgba(233, 243, 242, .4)', fontSize: 10 }}>{relativeActivityTime(event.created_at)}</Box>
             </Box>
           )
         })}
       </Box>
    </Paper>

    {/* Node detail drawer */}
    <Drawer anchor={small ? 'bottom' : 'right'} open={Boolean(selected)} onClose={() => setSelected(null)} PaperProps={{ sx: { p: 3, width: small ? '100%' : 360, background: 'rgba(12, 18, 24, .92)', backdropFilter: 'blur(15px)', borderLeft: small ? 0 : '1px solid rgba(106, 137, 156, .2)' } }}>
      {selected && <Box component="section" aria-label="Selected chart feature">
        <Stack direction="row" justifyContent="space-between" alignItems="start">
          <Box>
            <Typography variant="overline" color="primary">{selected.kind} · SURVEYED</Typography>
            <Typography variant="h4">{selected.label}</Typography>
          </Box>
          <IconButton aria-label="Close inspector" onClick={() => setSelected(null)} sx={{ color: 'text.secondary' }}><Close /></IconButton>
        </Stack>
        <Typography sx={{ mt: 2 }} color="text.secondary">{selected.member_count} {selected.member_count === 1 ? 'note' : 'notes'} in this bearing.</Typography>
        <Typography variant="body2" sx={{ mt: 1, fontFamily: '"IBM Plex Mono", monospace', overflowWrap: 'anywhere', color: 'text.secondary' }}>{selected.scope}</Typography>
        {view?.level !== 2 && <Button fullWidth sx={{ mt: 3, color: '#0b1117', background: '#e6bf69', fontWeight: 600, '&:hover': { background: '#d4ad55' } }} variant="contained" onClick={() => selectCluster(selected)}>Enter archive</Button>}
      </Box>}
    </Drawer>
  </Box>
}
