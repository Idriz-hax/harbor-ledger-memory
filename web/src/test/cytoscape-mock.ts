/* Minimal Cytoscape mock shared by the vitest setup file and graph tests. */

export type MockElementDefinition = {
  data: Record<string, unknown>
  position?: { x: number; y: number }
}

export type MockAnimation = {
  ids: string[]
  style: Record<string, unknown>
  duration?: number
}

type MockEl = MockElementDefinition & { classes: string[] }

const splitClasses = (value: string) => value.split(/\s+/).filter(Boolean)

export class MockCollection {
  constructor(private readonly els: MockEl[]) {}

  get length() {
    return this.els.length
  }

  filter(predicate: (ele: MockElement) => boolean) {
    return new MockCollection(this.els.filter(ele => predicate(wrapEl(ele))))
  }

  addClass(...classes: string[]) {
    const next = new Set(classes.flatMap(splitClasses))
    this.els.forEach(el => {
      el.classes = [...new Set([...el.classes, ...next])]
    })
  }

  removeClass(...classes: string[]) {
    const drop = new Set(classes.flatMap(splitClasses))
    this.els.forEach(el => {
      el.classes = el.classes.filter(c => !drop.has(c))
    })
  }

  hasClass(cls: string) {
    return this.els.length > 0 && this.els.every(el => el.classes.includes(cls))
  }

  /* Mirrors Collection.animate: records the call, completes synchronously
     (so bounded ping-pong animations run to termination deterministically),
     and returns the collection like the real API. */
  animate(options: { style?: Record<string, unknown>; duration?: number; complete?: () => void }) {
    if (this.els.length > 0) {
      cytoscapeMock.animations.push({
        ids: this.els.map(el => String(el.data.id)),
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
}

type MockElement = {
  data: (key?: string) => unknown
  id: () => string
  hasClass: (cls: string) => boolean
  addClass: (...classes: string[]) => void
  removeClass: (...classes: string[]) => void
}

function wrapEl(el: MockEl): MockElement {
  return {
    data: (key?: string) => (key === undefined ? el.data : el.data[key]),
    id: () => String(el.data.id),
    hasClass: (cls: string) => el.classes.includes(cls),
    addClass: (...classes: string[]) => new MockCollection([el]).addClass(...classes),
    removeClass: (...classes: string[]) => new MockCollection([el]).removeClass(...classes),
  }
}

const isEdge = (el: MockEl) => el.data.source !== undefined

export const cytoscapeMock = {
  instances: [] as { els: MockEl[] }[],
  options: undefined as Record<string, unknown> | undefined,
  createCalls: 0,
  destroyCalls: 0,
  animations: [] as MockAnimation[],

  get current() {
    return this.instances[this.instances.length - 1]
  },

  nodeIds(): string[] {
    return (this.current?.els ?? []).filter(el => !isEdge(el)).map(el => String(el.data.id))
  },

  edgeIds(): string[] {
    return (this.current?.els ?? []).filter(isEdge).map(el => String(el.data.id))
  },

  classesFor(id: string): string[] {
    return (this.current?.els ?? []).find(el => el.data.id === id)?.classes ?? []
  },

  nodes() {
    return new MockCollection((this.current?.els ?? []).filter(el => !isEdge(el)))
  },

  edges() {
    return new MockCollection((this.current?.els ?? []).filter(isEdge))
  },

  reset() {
    this.instances = []
    this.createCalls = 0
    this.destroyCalls = 0
    this.animations = []
    this.options = undefined
  },
}

export function createMockCore(options: { elements?: MockElementDefinition[]; [key: string]: unknown }) {
  cytoscapeMock.options = options
  const els: MockEl[] = (options.elements ?? []).map(el => ({
    data: { ...el.data },
    position: el.position,
    classes: [],
  }))
  const instance = {
    els,
    on: () => undefined,
    batch: (fn: () => void) => fn(),
    add: (added: MockElementDefinition | MockElementDefinition[]) => {
      const list = Array.isArray(added) ? added : [added]
      els.push(...list.map(el => ({ data: { ...el.data }, classes: [] })))
    },
    nodes: () => new MockCollection(els.filter(el => !isEdge(el))),
    edges: () => new MockCollection(els.filter(isEdge)),
    getElementById: (id: string) => new MockCollection(els.filter(el => el.data.id === id)),
    collection: () => new MockCollection(els),
    destroy() {
      cytoscapeMock.destroyCalls++
    },
  }
  cytoscapeMock.instances.push(instance)
  cytoscapeMock.createCalls++
  return instance
}
