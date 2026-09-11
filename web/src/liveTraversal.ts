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

type Trace = { timer: number | null; elements: Map<string, Set<string>> }

/** Owns only the visual classes introduced by one live trace. */
export class LiveTraversalController {
  private readonly traces = new Map<string, Trace>()
  private readonly sequences = new Map<string, number>()
  private readonly classOwners = new Map<string, Set<string>>()
  private disposed = false

  constructor(private readonly core: Core, private readonly options: { reducedMotion?: boolean; duration?: number } = {}) {}

  apply(event: LiveTraversalEvent) {
    if (this.disposed || !event.trace_id || !Number.isFinite(event.sequence)) return
    if ((this.sequences.get(event.trace_id) ?? -1) >= event.sequence) return
    this.sequences.set(event.trace_id, event.sequence)
    this.clearTrace(event.trace_id)

    const trace: Trace = { timer: null, elements: new Map() }
    this.traces.set(event.trace_id, trace)
    const node = this.core.getElementById(event.node_path)
    const nodeClass = event.mode === 'write' ? 'traversal-write' : 'traversal-read'
    this.addOwned(trace, node, nodeClass)

    if (event.source_path && event.target_path) {
      const edge = this.core.edges().filter(candidate => {
        const source = String(candidate.data('source') ?? '')
        const target = String(candidate.data('target') ?? '')
        const type = event.edge_type
        return source === event.source_path && target === event.target_path
          && (!type || String(candidate.data('edge_type') ?? candidate.data('type') ?? '') === type)
      })
      this.addOwned(trace, edge, 'traversal-forward')
      if (!this.options.reducedMotion && edge.length) {
        edge.animate({ style: { 'line-dash-offset': -13 }, duration: this.options.duration ?? 900 })
      }
    }

    trace.timer = window.setTimeout(() => this.clearTrace(event.trace_id), this.options.reducedMotion ? 0 : (this.options.duration ?? 900))
  }

  dispose() {
    if (this.disposed) return
    this.disposed = true
    for (const traceId of this.traces.keys()) this.clearTrace(traceId)
    this.traces.clear()
    this.sequences.clear()
  }

  private addOwned(trace: Trace, elements: { length: number; forEach: (fn: (element: any) => void) => unknown }, className: string) {
    elements.forEach(element => {
      const id = String(element.id())
      const key = `${id}:${className}`
      const owners = this.classOwners.get(key) ?? new Set<string>()
      owners.add(this.traceKey(trace, key))
      this.classOwners.set(key, owners)
      const classes = trace.elements.get(id) ?? new Set<string>()
      classes.add(className)
      trace.elements.set(id, classes)
      element.addClass(className)
    })
  }

  private traceKey(trace: Trace, key: string) { return `${this.traceIdFor(trace)}:${key}` }

  private traceIdFor(trace: Trace) {
    for (const [id, value] of this.traces) if (value === trace) return id
    return ''
  }

  private clearTrace(traceId: string) {
    const trace = this.traces.get(traceId)
    if (!trace) return
    if (trace.timer !== null) window.clearTimeout(trace.timer)
    for (const [id, classes] of trace.elements) {
      const element = this.core.getElementById(id)
      classes.forEach(className => {
        const key = `${id}:${className}`
        const owners = this.classOwners.get(key)
        owners?.delete(this.traceKey(trace, key))
        if (!owners || owners.size === 0) {
          element.removeClass(className)
          this.classOwners.delete(key)
        }
      })
    }
    this.traces.delete(traceId)
  }
}
