/* Minimal Cytoscape mock shared by the vitest setup file and graph tests. */

export type MockElementDefinition = {
  data: Record<string, unknown>
  position?: { x: number; y: number }
  locked?: boolean
  classes?: string[]
}

export type MockAnimation = {
  ids: string[]
  style: Record<string, unknown>
  duration?: number
}

type MockEl = {
  data: Record<string, unknown>
  position?: { x: number; y: number }
  classes: string[]
  locked: boolean
}

type MockElement = {
  data: (key?: string) => unknown
  position: (point?: { x: number; y: number }) => { x: number; y: number } | void
  lock: () => void
  unlock: () => void
  locked: () => boolean
  id: () => string
  hasClass: (cls: string) => boolean
  addClass: (...classes: string[]) => void
  removeClass: (...classes: string[]) => void
  animate: (options: { style?: Record<string, unknown>; duration?: number; complete?: () => void }) => void
}

const splitClasses = (value: string) => value.split(/\s+/).filter(Boolean)
const isEdge = (el: MockEl) => el.data.source !== undefined

export class MockCollection {
  constructor(
    private readonly els: MockEl[],
    private readonly onRemove?: (el: MockEl) => void,
  ) {}

  get length() {
    return this.els.length
  }

  filter(predicate: (ele: MockElement) => boolean) {
    return new MockCollection(this.els.filter((el) => predicate(wrapEl(el))), this.onRemove)
  }

  map<T>(predicate: (ele: MockElement) => T) {
    return this.els.map((el) => predicate(wrapEl(el)))
  }

  forEach(predicate: (ele: MockElement) => void) {
    this.els.forEach((el) => predicate(wrapEl(el)))
    return this
  }

  remove() {
    const removed = this.els.slice()
    for (const el of removed) this.onRemove?.(el)
    return new MockCollection(removed, this.onRemove)
  }

  addClass(...classes: string[]) {
    const next = new Set(classes.flatMap(splitClasses))
    this.els.forEach((el) => {
      el.classes = [...new Set([...el.classes, ...next])]
    })
  }

  removeClass(...classes: string[]) {
    const drop = new Set(classes.flatMap(splitClasses))
    this.els.forEach((el) => {
      el.classes = el.classes.filter((c) => !drop.has(c))
    })
  }

  hasClass(cls: string) {
    return this.els.length > 0 && this.els.every((el) => el.classes.includes(cls))
  }

  /* Mirrors Collection.animate: records the call, completes synchronously
     (so bounded ping-pong animations run to termination deterministically),
     and returns the collection like the real API. */
  animate(options: { style?: Record<string, unknown>; duration?: number; complete?: () => void }) {
    if (this.els.length > 0) {
      cytoscapeMock.animations.push({
        ids: this.els.map((el) => String(el.data.id)),
        style: { ...(options.style ?? {}) },
        duration: options.duration,
      })
    }
    options.complete?.()
    return this
  }

  stop() {
    return this
  }

  lock() {
    this.els.forEach(el => { el.locked = true })
    return this
  }

  unlock() {
    this.els.forEach(el => { el.locked = false })
    return this
  }
}

function wrapEl(el: MockEl): MockElement {
  return {
    data: (key?: string) => (key === undefined ? el.data : el.data[key]),
    position: (point) => {
      if (point) el.position = { ...point }
      return el.position ?? { x: 0, y: 0 }
    },
    lock: () => { el.locked = true },
    unlock: () => { el.locked = false },
    locked: () => el.locked,
    id: () => String(el.data.id),
    hasClass: (cls: string) => el.classes.includes(cls),
    addClass: (...classes: string[]) => new MockCollection([el]).addClass(...classes),
    removeClass: (...classes: string[]) => new MockCollection([el]).removeClass(...classes),
    animate: (options) => new MockCollection([el]).animate(options),
  }
}

export const cytoscapeMock = {
  instances: [] as { els: MockEl[] }[],
  options: undefined as Record<string, unknown> | undefined,
  createCalls: 0,
  destroyCalls: 0,
  zoomCalls: [] as number[],
  animations: [] as MockAnimation[],
  layoutCalls: [] as Record<string, unknown>[],
  layoutStopCallbacks: [] as (() => void)[],
  fitCalls: 0,
  centerCalls: 0,
  resizeCalls: 0,

  get current() {
    return this.instances[this.instances.length - 1]
  },

  nodeIds(): string[] {
    return (this.current?.els ?? []).filter((el) => !isEdge(el)).map((el) => String(el.data.id))
  },

  positionFor(id: string) {
    return this.current?.els.find(el => el.data.id === id)?.position
  },

  node(id: string) {
    const element = this.current?.els.find(el => el.data.id === id)
    if (!element) throw new Error(`mock node not found: ${id}`)
    return wrapEl(element)
  },

  lockedFor(id: string) {
    return this.current?.els.find(el => el.data.id === id)?.locked ?? false
  },

  runLastLayoutStop() {
    this.layoutStopCallbacks[this.layoutStopCallbacks.length - 1]?.()
  },

  edgeIds(): string[] {
    return (this.current?.els ?? []).filter(isEdge).map((el) => String(el.data.id))
  },

  classesFor(id: string): string[] {
    return (this.current?.els ?? []).find((el) => el.data.id === id)?.classes ?? []
  },

  nodes() {
    return new MockCollection((this.current?.els ?? []).filter((el) => !isEdge(el)))
  },

  edges() {
    return new MockCollection((this.current?.els ?? []).filter(isEdge))
  },

  reset() {
    this.instances = []
    this.createCalls = 0
    this.destroyCalls = 0
    this.zoomCalls = []
    this.animations = []
    this.layoutCalls = []
    this.layoutStopCallbacks = []
    this.fitCalls = 0
    this.centerCalls = 0
    this.resizeCalls = 0
    this.options = undefined
  },
}

type MockCore = {
  els: MockEl[]
  handlers: Record<string, ((event: unknown) => void)[]>
  zoomValue: number
  panValue: { x: number; y: number }
  styleList: Record<string, unknown>[]
  on: (type: string, selectorOrHandler: string | ((event: unknown) => void), handler?: (event: unknown) => void) => void
  off: () => void
  batch: (fn: () => void) => MockCore
  add: (added: MockElementDefinition | MockElementDefinition[]) => MockCore
  nodes: () => MockCollection
  edges: () => MockCollection
  elements: () => MockCollection
  getElementById: (id: string) => MockCollection
  collection: () => MockCollection
  zoom: (value?: number) => number | MockCore
  pan: (value?: { x: number; y: number }) => { x: number; y: number } | MockCore
  fit: (eles?: unknown, padding?: number) => MockCore
  resize: () => MockCore
  layout: (options: Record<string, unknown>) => { run: () => void }
  center: (target?: unknown) => MockCore
  animate: (settings: Record<string, unknown>) => MockCore
  stop: () => MockCore
  style: (styles: Record<string, unknown>[]) => MockCore
  destroy: () => void
}

export function createMockCore(options: { elements?: MockElementDefinition[]; [key: string]: unknown }): MockCore {
  cytoscapeMock.options = options
  const els: MockEl[] = (options.elements ?? []).map((el) => ({
    data: { ...el.data },
    position: el.position,
    classes: [...(el.classes ?? [])],
    locked: Boolean((el as MockElementDefinition & { locked?: boolean }).locked),
  }))
  const handlers: Record<string, ((event: unknown) => void)[]> = {}

  const collection = (list: MockEl[]) => new MockCollection(list, (el) => {
    const idx = els.indexOf(el)
    if (idx >= 0) els.splice(idx, 1)
  })

  const instance: MockCore = {
    els,
    handlers,
    zoomValue: 1,
    panValue: { x: 0, y: 0 },
    styleList: (options.style as Record<string, unknown>[]) ?? [],
    on(type, selectorOrHandler, handler) {
      const fn = typeof selectorOrHandler === 'function' ? selectorOrHandler : handler
      if (typeof fn !== 'function') return
      for (const key of type.split(/\s+/).filter(Boolean)) {
        handlers[key] = [...(handlers[key] ?? []), fn]
      }
    },
    off() {
      for (const key of Object.keys(handlers)) delete handlers[key]
    },
    batch(fn) {
      fn()
      return instance
    },
    add(added) {
      const list = Array.isArray(added) ? added : [added]
      for (const el of list) els.push({ data: { ...el.data }, position: el.position, classes: [...(el.classes ?? [])], locked: Boolean((el as MockElementDefinition & { locked?: boolean }).locked) })
      return instance
    },
    nodes: () => collection(els.filter((el) => !isEdge(el))),
    edges: () => collection(els.filter(isEdge)),
    elements: () => collection(els.slice()),
    getElementById: (id) => collection(els.filter((el) => el.data.id === id)),
    collection: () => collection(els.slice()),
    zoom(value) {
      if (value === undefined) return instance.zoomValue
      instance.zoomValue = value
      cytoscapeMock.zoomCalls.push(value)
      return instance
    },
    pan(value) {
      if (value === undefined) return instance.panValue
      instance.panValue = { ...value }
      return instance
    },
    fit() {
      cytoscapeMock.fitCalls++
      return instance
    },
    resize() {
      cytoscapeMock.resizeCalls++
      return instance
    },
    layout(options) {
      cytoscapeMock.layoutCalls.push(options)
      if (typeof options.stop === 'function') cytoscapeMock.layoutStopCallbacks.push(options.stop as () => void)
      return { run: () => undefined }
    },
    center() {
      cytoscapeMock.centerCalls++
      return instance
    },
    animate(settings) {
      cytoscapeMock.animations.push({ ids: [], style: { ...settings }, duration: settings.duration as number | undefined })
      return instance
    },
    stop() {
      return instance
    },
    style(styles) {
      instance.styleList = styles
      return instance
    },
    destroy() {
      cytoscapeMock.destroyCalls++
    },
  }
  cytoscapeMock.instances.push(instance)
  cytoscapeMock.createCalls++
  return instance
}

/* Fire a registered event handler on the most recent core (used to simulate
   pan/zoom/tap gestures without a real Cytoscape renderer). */
export function emitMockEvent(type: string, event: Record<string, unknown> = {}) {
  const instance = cytoscapeMock.current as MockCore | undefined
  for (const handler of instance?.handlers[type] ?? []) handler(event)
}

export function setMockZoom(zoom: number) {
  const instance = cytoscapeMock.current as MockCore | undefined
  if (instance) instance.zoomValue = zoom
}
