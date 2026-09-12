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
  timer: number | null
  elements: Map<string, Set<string>>
}

type TraceState = { pulses: Set<Pulse> }

const DISPATCH_INTERVAL = 350
const HALO_DURATION = 1200
const PENDING_EXPIRY = 2000
const MAX_QUEUE = 3

/** Owns only the visual classes introduced by one live trace. */
export class LiveTraversalController {
  private readonly traces = new Map<string, TraceState>()
  private readonly sequences = new Map<string, number>()
  private readonly pending = new Map<string, LiveTraversalEvent>()
  private readonly pendingTimers = new Map<string, number>()
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
    if ((this.sequences.get(event.trace_id) ?? -1) >= event.sequence
      || (this.pending.get(event.trace_id)?.sequence ?? -1) >= event.sequence
      || (this.queues.get(event.trace_id)?.[this.queues.get(event.trace_id)!.length - 1]?.sequence ?? -1) >= event.sequence) return
    const queue = this.queues.get(event.trace_id) ?? []
    queue.push(event)
    if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - 1)
    this.queues.set(event.trace_id, queue)
    this.addTrace(event.trace_id)
    this.reportActivityChange()
    this.startPump()
  }

  /** Retry events that arrived while the current graph view was still resolving. */
  flush() {
    if (this.disposed || !this.pending.size) return
    const pending = [...this.pending.values()]
    pending.forEach(event => this.clearPending(event.trace_id))
    pending.forEach(event => this.apply(event))
  }

  activeTraceCount() {
    const active = new Set([...this.traces.keys(), ...this.queues.keys(), ...this.pending.keys()])
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
      if (!this.render(event)) this.deferPending(event)
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
    const nodeId = this.options.nodeIdForPath?.(event.node_path) ?? event.node_path
    const node = this.core.getElementById(nodeId)
    if (!node.length) return false
    let edge: { length: number; forEach: (fn: (element: any) => void) => unknown; animate: (options: { style: Record<string, unknown>; duration: number }) => unknown } | undefined
    if (event.source_path && event.target_path) {
      const edgeId = this.options.edgeIdForEvent?.(event)
      edge = this.options.edgeIdForEvent
        ? edgeId ? this.core.getElementById(edgeId) : this.core.edges().filter(() => false)
        : this.core.edges().filter(candidate => String(candidate.data('source') ?? '') === event.source_path
          && String(candidate.data('target') ?? '') === event.target_path
          && (!event.edge_type || String(candidate.data('edge_type') ?? candidate.data('type') ?? '') === event.edge_type))
      if (!edge.length) {
        return false
      }
    }

    const pulse: Pulse = { id: ++this.pulseId, timer: null, elements: new Map() }
    const trace = this.traces.get(event.trace_id) ?? { pulses: new Set<Pulse>() }
    trace.pulses.add(pulse)
    this.traces.set(event.trace_id, trace)
    this.sequences.set(event.trace_id, event.sequence)
    this.addOwned(event.trace_id, pulse, node, event.mode === 'write' ? 'traversal-write' : 'traversal-read')

    if (event.source_path && event.target_path) {
      this.addOwned(event.trace_id, pulse, edge!, 'traversal-forward')
      if (!this.options.reducedMotion && edge!.length) {
        edge!.animate({ style: { 'line-dash-offset': -13 }, duration: this.options.duration ?? 900 })
      }
    }

    pulse.timer = window.setTimeout(() => this.clearPulse(event.trace_id, pulse), HALO_DURATION)
    return true
  }

  private deferPending(event: LiveTraversalEvent) {
    this.pending.set(event.trace_id, event)
    const previous = this.pendingTimers.get(event.trace_id)
    if (previous !== undefined) window.clearTimeout(previous)
    const timer = window.setTimeout(() => {
      if (this.pending.get(event.trace_id) !== event) return
      this.clearPending(event.trace_id)
      this.pruneTraceOrder()
      this.reportActivityChange()
    }, PENDING_EXPIRY)
    this.pendingTimers.set(event.trace_id, timer)
  }

  private clearPending(traceId: string) {
    this.pending.delete(traceId)
    const timer = this.pendingTimers.get(traceId)
    if (timer !== undefined) window.clearTimeout(timer)
    this.pendingTimers.delete(traceId)
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
    for (const timer of this.pendingTimers.values()) window.clearTimeout(timer)
    this.pendingTimers.clear()
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

  private clearPulse(traceId: string, pulse: Pulse) {
    const trace = this.traces.get(traceId)
    if (!trace || !trace.pulses.delete(pulse)) return
    if (pulse.timer !== null) window.clearTimeout(pulse.timer)
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
