import type { Core } from 'cytoscape'

export type LiveTraversalEvent = {
  trace_id: string
  sequence: number
  mode: 'read' | 'write' | string
  node_path: string
  source_path?: string
  target_path?: string
  edge_type?: string
}

export type LiveTraversalOptions = {
  reducedMotion?: boolean
  duration?: number
  nodeIdForPath?: (path: string) => string | undefined
  edgeIdForEvent?: (event: LiveTraversalEvent) => string | undefined
  onActivityChange?: (activeTraceCount: number) => void
}

type Pulse = {
  id: number
  haloTimer: number | null
  pulseTimer: number | null
  elements: Map<string, Set<string>>
  pulseElements: Set<string>
}

type TraceState = { pulses: Set<Pulse> }

const DISPATCH_INTERVAL = 350
const HALO_DURATION = 1200
const NODE_PULSE_DURATION = 175
const MAX_QUEUE = 3
export const MAX_PENDING_EVENTS = 32
export const MAX_SEQUENCE_TOMBSTONES = 256
export const SEQUENCE_TOMBSTONE_TTL = 30_000

type SequenceTombstone = { sequence: number; expiresAt: number }

/** Owns only the visual classes introduced by one live trace. */
export class LiveTraversalController {
  private readonly traces = new Map<string, TraceState>()
  private readonly sequences = new Map<string, number>()
  private readonly tombstones = new Map<string, SequenceTombstone>()
  private readonly pending = new Map<string, LiveTraversalEvent>()
  private readonly queues = new Map<string, LiveTraversalEvent[]>()
  private readonly traceOrder: string[] = []
  private readonly classOwners = new Map<string, Set<string>>()
  private pump: number | null = null
  private traceCursor = 0
  private pulseId = 0
  private disposed = false
  private lastReportedActivityCount = 0

  constructor(private readonly core: Core, private readonly options: LiveTraversalOptions = {}) {}

  setReducedMotion(reducedMotion: boolean) { this.options.reducedMotion = reducedMotion }

  apply(event: LiveTraversalEvent) {
    if (this.disposed || !event.trace_id || !Number.isFinite(event.sequence)) return
    this.pruneTombstones()
    if ((this.sequences.get(event.trace_id) ?? -1) >= event.sequence
      || (this.tombstones.get(event.trace_id)?.sequence ?? -1) >= event.sequence
      || (this.pending.get(event.trace_id)?.sequence ?? -1) >= event.sequence
      || (this.queues.get(event.trace_id)?.[this.queues.get(event.trace_id)!.length - 1]?.sequence ?? -1) >= event.sequence) return
    const queue = this.queues.get(event.trace_id) ?? []
    queue.push(event)
    if (queue.length > MAX_QUEUE) {
      const latestRenderable = [...queue].reverse().find(candidate => this.isRenderable(candidate))
      queue.splice(0, queue.length, latestRenderable ?? event)
    }
    this.queues.set(event.trace_id, queue)
    this.addTrace(event.trace_id)
    this.reportActivityChange()
    this.startPump()
  }

  /** Retry events that arrived while the current graph view was still resolving. */
  flush() {
    if (this.disposed || !this.pending.size) return
    const pending = [...this.pending.values()]
    this.pending.clear()
    pending.forEach(event => this.apply(event))
  }

  activeTraceCount() {
    const active = new Set([...this.traces.keys(), ...this.queues.keys()])
    return active.size
  }

  private reportActivityChange() {
    const count = this.activeTraceCount()
    if (count === this.lastReportedActivityCount) return
    this.lastReportedActivityCount = count
    this.options.onActivityChange?.(count)
  }

  private addTrace(traceId: string) {
    if (!this.traceOrder.includes(traceId)) this.traceOrder.push(traceId)
  }

  private startPump() {
    if (this.pump !== null || this.disposed) return
    this.pump = window.setTimeout(() => {
      this.pump = null
      this.dispatchNext()
    }, DISPATCH_INTERVAL)
  }

  private dispatchNext() {
    if (this.disposed) return
    const traceId = this.nextQueuedTrace()
    if (traceId) {
      const queue = this.queues.get(traceId)!
      const event = queue.shift()!
      if (!queue.length) this.queues.delete(traceId)
      if (!this.render(event)) {
        this.retainPending(event)
        this.reportActivityChange()
      }
      this.pruneTraceOrder()
    }
    if (this.queues.size) this.startPump()
  }

  private nextQueuedTrace() {
    if (!this.traceOrder.length) return undefined
    for (let offset = 0; offset < this.traceOrder.length; offset++) {
      const index = (this.traceCursor + offset) % this.traceOrder.length
      const traceId = this.traceOrder[index]
      if (this.queues.has(traceId)) {
        this.traceCursor = (index + 1) % this.traceOrder.length
        return traceId
      }
    }
    return undefined
  }

  private pruneTraceOrder() {
    for (let index = this.traceOrder.length - 1; index >= 0; index--) {
      const traceId = this.traceOrder[index]
      if (!this.queues.has(traceId) && !this.pending.has(traceId) && !this.traces.has(traceId)) {
        this.traceOrder.splice(index, 1)
        if (index < this.traceCursor) this.traceCursor--
      }
    }
    if (this.traceOrder.length) this.traceCursor %= this.traceOrder.length
    else this.traceCursor = 0
  }

  private render(event: LiveTraversalEvent) {
    if ((this.sequences.get(event.trace_id) ?? -1) >= event.sequence) return true
    const resolved = this.resolveElements(event)
    if (!resolved) return false
    const { node, edge } = resolved
    this.tombstones.delete(event.trace_id)
    const pulse: Pulse = { id: ++this.pulseId, haloTimer: null, pulseTimer: null, elements: new Map(), pulseElements: new Set() }
    const trace = this.traces.get(event.trace_id) ?? { pulses: new Set<Pulse>() }
    trace.pulses.add(pulse)
    this.traces.set(event.trace_id, trace)
    this.sequences.set(event.trace_id, event.sequence)
    this.addOwned(event.trace_id, pulse, node, event.mode === 'write' ? 'traversal-write' : 'traversal-read')
    if (!this.options.reducedMotion) {
      this.addOwned(event.trace_id, pulse, node, 'traversal-pulse')
      node.forEach(element => { pulse.pulseElements.add(String(element.id())) })
      pulse.pulseTimer = window.setTimeout(() => this.clearPulseStyle(event.trace_id, pulse), NODE_PULSE_DURATION)
    }

    if (edge) {
      const edgeClass = event.mode === 'write' ? 'traversal-forward-write' : 'traversal-forward'
      this.addOwned(event.trace_id, pulse, edge, edgeClass)
      if (!this.options.reducedMotion && edge.length) {
        edge.animate({ style: { 'line-dash-offset': -13 }, duration: this.options.duration ?? 900 })
      }
    }

    pulse.haloTimer = window.setTimeout(() => this.clearPulse(event.trace_id, pulse), HALO_DURATION)
    return true
  }

  private isRenderable(event: LiveTraversalEvent) {
    return Boolean(this.resolveElements(event))
  }

  private retainPending(event: LiveTraversalEvent) {
    this.pending.set(event.trace_id, event)
    while (this.pending.size > MAX_PENDING_EVENTS) {
      const oldestTraceId = this.pending.keys().next().value as string | undefined
      if (oldestTraceId === undefined) break
      const evicted = this.pending.get(oldestTraceId)
      this.pending.delete(oldestTraceId)
      if (evicted) this.recordTombstone(oldestTraceId, evicted.sequence)
    }
    this.pruneTraceOrder()
  }

  private resolveElements(event: LiveTraversalEvent) {
    const nodeId = this.options.nodeIdForPath?.(event.node_path) ?? event.node_path
    const node = this.core.getElementById(nodeId)
    if (!node.length) return undefined
    let edge: { length: number; forEach: (fn: (element: any) => void) => unknown; animate: (options: { style: Record<string, unknown>; duration: number }) => unknown } | undefined
    if (event.source_path && event.target_path) {
      const edgeId = this.options.edgeIdForEvent?.(event)
      edge = this.options.edgeIdForEvent
        ? edgeId ? this.core.getElementById(edgeId) : this.core.edges().filter(() => false)
        : this.core.edges().filter(candidate => String(candidate.data('source') ?? '') === event.source_path
          && String(candidate.data('target') ?? '') === event.target_path
          && (!event.edge_type || String(candidate.data('edge_type') ?? candidate.data('type') ?? '') === event.edge_type))
      if (!edge.length) edge = undefined
    }
    return { node, edge }
  }

  private recordTombstone(traceId: string, sequence: number) {
    this.pruneTombstones()
    const current = this.tombstones.get(traceId)?.sequence ?? -1
    this.tombstones.delete(traceId)
    this.tombstones.set(traceId, {
      sequence: Math.max(current, sequence),
      expiresAt: Date.now() + SEQUENCE_TOMBSTONE_TTL,
    })
    while (this.tombstones.size > MAX_SEQUENCE_TOMBSTONES) {
      const oldest = this.tombstones.keys().next().value as string | undefined
      if (oldest === undefined) break
      this.tombstones.delete(oldest)
    }
  }

  private pruneTombstones() {
    const now = Date.now()
    for (const [traceId, tombstone] of this.tombstones) {
      if (tombstone.expiresAt <= now) this.tombstones.delete(traceId)
    }
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    if (this.pump !== null) window.clearTimeout(this.pump)
    this.pump = null
    for (const traceId of this.traces.keys()) this.clearTrace(traceId)
    this.traces.clear()
    this.queues.clear()
    this.sequences.clear()
    this.tombstones.clear()
    this.pending.clear()
    this.traceOrder.length = 0
  }

  private addOwned(traceId: string, pulse: Pulse, elements: { length: number; forEach: (fn: (element: any) => void) => unknown }, className: string) {
    elements.forEach(element => {
      const id = String(element.id())
      const key = `${id}:${className}`
      const owners = this.classOwners.get(key) ?? new Set<string>()
      owners.add(`${traceId}:${pulse.id}`)
      this.classOwners.set(key, owners)
      const classes = pulse.elements.get(id) ?? new Set<string>()
      classes.add(className)
      pulse.elements.set(id, classes)
      element.addClass(className)
    })
  }

  private clearPulseStyle(traceId: string, pulse: Pulse) {
    if (!pulse.pulseElements.size) return
    for (const id of pulse.pulseElements) this.removeOwned(traceId, pulse, id, 'traversal-pulse')
    pulse.pulseElements.clear()
  }

  private removeOwned(traceId: string, pulse: Pulse, id: string, className: string) {
    const key = `${id}:${className}`
    const owners = this.classOwners.get(key)
    owners?.delete(`${traceId}:${pulse.id}`)
    if (!owners || owners.size === 0) {
      this.core.getElementById(id).removeClass(className)
      this.classOwners.delete(key)
    }
    pulse.elements.get(id)?.delete(className)
  }

  private clearPulse(traceId: string, pulse: Pulse) {
    const trace = this.traces.get(traceId)
    if (!trace || !trace.pulses.delete(pulse)) return
    if (pulse.haloTimer !== null) window.clearTimeout(pulse.haloTimer)
    if (pulse.pulseTimer !== null) window.clearTimeout(pulse.pulseTimer)
    for (const [id, classes] of pulse.elements) {
      const element = this.core.getElementById(id)
      classes.forEach(className => {
        const key = `${id}:${className}`
        const owners = this.classOwners.get(key)
        owners?.delete(`${traceId}:${pulse.id}`)
        if (!owners || owners.size === 0) {
          element.removeClass(className)
          this.classOwners.delete(key)
        }
      })
    }
    if (!trace.pulses.size) {
      this.traces.delete(traceId)
      const sequence = this.sequences.get(traceId)
      this.sequences.delete(traceId)
      if (sequence !== undefined) this.recordTombstone(traceId, sequence)
      this.pruneTraceOrder()
      this.reportActivityChange()
    }
  }

  private clearTrace(traceId: string) {
    const trace = this.traces.get(traceId)
    if (!trace) return
    for (const pulse of [...trace.pulses]) this.clearPulse(traceId, pulse)
  }
}
