import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TideAtlas, activityTarget, folderGroupColor, idleFloatOffset, nodeMotionPhase, readableActivityType, relativeActivityTime, topLevelFolder } from '../TideAtlas'
import { cytoscapeMock, emitMockEvent, setMockZoom } from '../test/cytoscape-mock'

const cluster = (id: string, scope: string, label = id) => ({ id, label, kind: 'file', type_counts: { file: 1 }, member_count: 1, scope })
const view = (level: number, scope: string | null, clusters = [cluster('harbor', 'AI')]) => ({ level, scope, clusters, edges: [], generation: `level-${level}` })

function mockAtlas(responses: unknown[]) {
  let viewCall = 0
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/settings')) return { ok: true, json: async () => ({ theme: { preset: 'deepwater' } }) } as Response
    if (url.includes('/api/v1/graph/view')) return { ok: true, json: async () => responses[Math.min(viewCall++, responses.length - 1)] } as Response
    if (url.includes('/api/v1/scan')) return { ok: true, json: async () => ({ accepted: true }) } as Response
    return { ok: true, json: async () => ({}) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('TideAtlas controls and viewport', () => {
  it('assigns stable accessible colors by top-level folder', () => {
    expect(topLevelFolder('AI/Notes/Harbor.md')).toBe('AI')
    expect(topLevelFolder('/')).toBe('(root)')
    expect(folderGroupColor('AI')).toBe(folderGroupColor('AI'))
    expect(folderGroupColor('AI')).not.toBe(folderGroupColor('Projects'))
  })

  it('assigns motion phases from node identity rather than label length', () => {
    expect(nodeMotionPhase('one')).not.toEqual(nodeMotionPhase('two'))
    expect(nodeMotionPhase('abc')).not.toEqual(nodeMotionPhase('xyz'))
  })

  it('keeps idle float subtle and on long, node-specific periods', () => {
    const phase = nodeMotionPhase('harbor')
    const samples = Array.from({ length: 240 }, (_, index) => idleFloatOffset('harbor', index / 10))
    expect(phase.periodX).toBeGreaterThanOrEqual(20)
    expect(phase.periodX).toBeLessThanOrEqual(35)
    expect(phase.periodY).toBeGreaterThanOrEqual(20)
    expect(phase.periodY).toBeLessThanOrEqual(35)
    expect(Math.max(...samples.map(sample => Math.abs(sample.x)))).toBeLessThanOrEqual(0.75)
    expect(Math.max(...samples.map(sample => Math.abs(sample.y)))).toBeLessThanOrEqual(0.75)
    expect(idleFloatOffset('harbor', phase.periodX)).toEqual(expect.objectContaining({ x: expect.closeTo(idleFloatOffset('harbor', 0).x, 10) }))
  })

  it('fits the initial graph once after all first-view nodes render', async () => {
    const fetchMock = mockAtlas([view(2, null, [cluster('first', 'AI'), cluster('second', 'Projects')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toEqual(expect.arrayContaining(['first', 'second'])))
    expect(cytoscapeMock.fitCalls).toBe(1)
    expect(cytoscapeMock.centerCalls).toBe(0)
    expect(cytoscapeMock.zoomCalls).toHaveLength(0)
    expect(cytoscapeMock.layoutCalls).toHaveLength(0)
    await new Promise(resolve => setTimeout(resolve, 450))
    expect(fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view'))).toHaveLength(1)
  })

  it('does not autonomously change node positions while idle', async () => {
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const node = cytoscapeMock.node('harbor')
    node.position({ x: 48, y: -24 })
    await new Promise(resolve => setTimeout(resolve, 650))
    expect(cytoscapeMock.positionFor('harbor')).toEqual({ x: 48, y: -24 })
  })
  it('zooms through Cytoscape without rebuilding the chart', async () => {
    mockAtlas([view(1, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Zoom in' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(cytoscapeMock.zoomCalls).toHaveLength(1)
    expect(cytoscapeMock.createCalls).toBe(1)
  })

  it('labels every top-left chart control with a tooltip', async () => {
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter fullscreen' })).toBeEnabled())

    for (const label of ['Zoom in', 'Zoom out', 'Rearrange layout', 'Reset view', 'Enter fullscreen']) {
      const button = screen.getByRole('button', { name: label })
      fireEvent.mouseOver(button)
      expect(await screen.findByRole('tooltip', { name: label })).toBeInTheDocument()
      fireEvent.mouseOut(button)
    }
  })

  it('uses the current Cytoscape zoom for predictable repeated zoom controls', async () => {
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Zoom in' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    expect(cytoscapeMock.zoomCalls).toEqual([1.25, 1.5625, 1.25])
  })

  it('resets layout and viewport with a guarded fit and no graph request', async () => {
    const fetchMock = mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const fits = cytoscapeMock.fitCalls
    fireEvent.click(screen.getByRole('button', { name: 'Reset view' }))
    expect(cytoscapeMock.fitCalls).toBe(fits + 1)
    await new Promise(resolve => setTimeout(resolve, 450))
    expect(fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view'))).toHaveLength(1)
  })

  it('uses the browser fullscreen API and resizes Cytoscape on transitions', async () => {
    mockAtlas([view(2, null)])
    let fullscreenElement: Element | null = null
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fullscreenElement })
    const requestFullscreen = vi.fn(async function (this: Element) {
      fullscreenElement = this
      document.dispatchEvent(new Event('fullscreenchange'))
    })
    const exitFullscreen = vi.fn(async () => {
      fullscreenElement = null
      document.dispatchEvent(new Event('fullscreenchange'))
    })
    Object.defineProperty(document, 'exitFullscreen', { configurable: true, value: exitFullscreen })
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter fullscreen' })).toBeEnabled())
    const chart = document.querySelector('.tide-atlas') as HTMLElement
    Object.defineProperty(chart, 'requestFullscreen', { configurable: true, value: requestFullscreen })
    fireEvent.click(screen.getByRole('button', { name: 'Enter fullscreen' }))
    await waitFor(() => expect(requestFullscreen).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Exit fullscreen' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Exit fullscreen' }))
    await waitFor(() => expect(exitFullscreen).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(cytoscapeMock.resizeCalls).toBe(2))
  })

  it('leaves fullscreen state unchanged when the browser API is unavailable', async () => {
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter fullscreen' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Enter fullscreen' }))
    expect(screen.getByRole('button', { name: 'Enter fullscreen' })).toBeInTheDocument()
  })

  it('does not run a global layout on load', async () => {
    mockAtlas([view(2, null, [cluster('harbor', 'AI'), cluster('second', 'Projects')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    expect(cytoscapeMock.layoutCalls).toHaveLength(0)
  })

  it('loads every graph page instead of dropping the cursor remainder', async () => {
    const fetchMock = mockAtlas([
      { ...view(1, null, [cluster('first', 'AI')]), next_cursor: 'cursor-1' },
      view(1, null, [cluster('second', 'Projects')]),
    ])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toEqual(expect.arrayContaining(['first', 'second'])))
    const graphCalls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view'))
    expect(graphCalls).toHaveLength(2)
    expect(String(graphCalls[1][0])).toContain('cursor=cursor-1')
  })

  it('rescans through the scan endpoint and reloads the bounded view', async () => {
    const fetchMock = mockAtlas([view(1, null), view(1, null, [cluster('new-harbor', 'Projects')])])
    render(<TideAtlas events={[]} onRefresh={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /rescan/i })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /rescan/i }))
    await waitFor(() => expect(fetchMock.mock.calls.some(call => String(call[0]).includes('/api/v1/scan') && call[1]?.method === 'POST')).toBe(true))
    await waitFor(() => expect(fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view')).length).toBe(2))
  })

  it('updates in place across a LOD boundary without refitting', async () => {
    mockAtlas([view(1, null), view(0, null, [cluster('coast', 'AI')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    setMockZoom(.1)
    emitMockEvent('zoom')
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('coast'))
    expect(cytoscapeMock.createCalls).toBe(1)
    expect((cytoscapeMock.current as unknown as { zoom?: () => number })?.zoom?.()).toBeCloseTo(.1)
  })

  it('does not let scoped file views trigger automatic LOD swaps', async () => {
    mockAtlas([view(1, null, [cluster('archive', 'AI/Knowledge')]), view(2, 'AI/Knowledge', [cluster('note', 'AI/Knowledge/Note.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('archive'))
    emitMockEvent('tap', { target: { id: () => 'archive' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Enter archive' }))
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('note'))
    setMockZoom(.2)
    emitMockEvent('zoom')
    await new Promise(resolve => setTimeout(resolve, 450))
    expect(cytoscapeMock.nodeIds()).toContain('note')
  })

  it('keeps an older graph response from replacing the newest view', async () => {
    let resolveInitial!: (value: Response) => void
    let resolveOlder!: (value: Response) => void
    let resolveNewer!: (value: Response) => void
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/graph/view')) {
        if (fetchMock.mock.calls.length === 1) return new Promise<Response>(resolve => { resolveInitial = resolve })
        if (fetchMock.mock.calls.length === 2) return new Promise<Response>(resolve => { resolveOlder = resolve })
        return new Promise<Response>(resolve => { resolveNewer = resolve })
      }
      return Promise.resolve({ ok: true, json: async () => ({}) } as Response)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(resolveInitial).toBeTypeOf('function'))
    resolveInitial({ ok: true, json: async () => view(1, null, [cluster('initial', 'AI')]) } as Response)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('initial'))
    setMockZoom(.1)
    emitMockEvent('zoom')
    await new Promise(resolve => setTimeout(resolve, 450))
    setMockZoom(.8)
    emitMockEvent('zoom')
    await waitFor(() => expect(resolveNewer).toBeTypeOf('function'))
    resolveNewer({ ok: true, json: async () => view(2, null, [cluster('newer', 'AI/New')]) } as Response)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('newer'))
    resolveOlder({ ok: true, json: async () => view(0, null, [cluster('older', 'AI/Old')]) } as Response)
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(cytoscapeMock.nodeIds()).not.toContain('initial')
    expect(cytoscapeMock.nodeIds()).not.toContain('older')
  })

  it('enters an archive at level 2 and keeps the selected bearing', async () => {
    const fetchMock =     mockAtlas([view(1, null, [cluster('archive', 'AI/Knowledge', 'Knowledge')]), view(2, 'AI/Knowledge', [cluster('note', 'AI/Knowledge/Note.md', 'Note')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('archive'))
    emitMockEvent('tap', { target: { id: () => 'archive' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Enter archive' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Enter archive' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view')).length).toBe(2))
    const graphCalls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view'))
    expect(graphCalls[1][0]).toContain('level=2')
    expect(graphCalls[1][0]).toContain('scope=AI%2FKnowledge')
    expect(screen.getByText('AI/Knowledge')).toBeInTheDocument()
  })

  it('clears a stale scope and reloads the whole vault after a scoped 400', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/graph/view') && url.includes('scope=')) {
        return {
          ok: false,
          status: 400,
          statusText: 'Bad Request',
          text: async () => JSON.stringify({ detail: 'scope is not accessible or contains no notes' }),
        } as Response
      }
      if (url.includes('/api/v1/graph/view')) {
        const wholeVault = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view')).length > 1
        return { ok: true, json: async () => wholeVault ? view(2, null, [cluster('whole', 'AI')]) : view(1, null, [cluster('archive', 'AI/Knowledge')]) } as Response
      }
      return { ok: true, json: async () => ({}) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('archive'))
    emitMockEvent('tap', { target: { id: () => 'archive' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Enter archive' }))

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('whole'))
    const graphCalls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view'))
    expect(graphCalls).toHaveLength(3)
    expect(String(graphCalls[2][0])).not.toContain('scope=')
    expect(screen.queryByText('scope is not accessible or contains no notes')).not.toBeInTheDocument()
  })

  it('does not run a layout after pan, zoom, or selection', async () => {
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'AI/Two.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('one'))
    const layouts = cytoscapeMock.layoutCalls.length
    emitMockEvent('pan')
    emitMockEvent('zoom')
    emitMockEvent('tap', { target: { id: () => 'one' } })
    await new Promise(resolve => setTimeout(resolve, 450))
    expect(cytoscapeMock.layoutCalls).toHaveLength(layouts)
  })

  it('restores a locally dragged node position after remount', async () => {
    localStorage.clear()
    mockAtlas([view(2, null)])
    const rendered = render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const node = cytoscapeMock.node('harbor')
    emitMockEvent('grab', { target: node })
    node.position({ x: 48, y: -24 })
    emitMockEvent('dragfree', { target: node })
    rendered.unmount()
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.positionFor('harbor')).toEqual({ x: 48, y: -24 }))
  })

  it('ignores positions from the previous layout storage version', async () => {
    localStorage.setItem('hlm:tide-atlas:positions:level-2', JSON.stringify({ harbor: { x: 999, y: 999 } }))
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    expect(cytoscapeMock.positionFor('harbor')).not.toEqual({ x: 999, y: 999 })
  })

  it('renders compact activity details and an empty state', async () => {
    const createdAt = new Date(Date.now() - 65_000).toISOString()
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[{ id: 4, event_type: 'vault.mutation.applied', created_at: createdAt, payload: { path: 'AI/Notes/Harbor.md' } }]} />)
    expect(screen.getByText('1 event')).toBeInTheDocument()
    expect(screen.getByText('Vault Mutation Applied')).toBeInTheDocument()
    expect(screen.getByText('AI/Notes/Harbor.md')).toBeInTheDocument()
    expect(screen.getByText('1m ago')).toBeInTheDocument()

    const empty = render(<TideAtlas events={[]} />)
    expect(screen.getByText('No recent activity')).toBeInTheDocument()
    empty.unmount()
  })

  it('formats activity values without exposing raw event names', () => {
    const event = { id: 1, event_type: 'vault.mutation.applied', created_at: new Date().toISOString(), payload: { note_path: 'A.md' } }
    expect(readableActivityType(event.event_type)).toBe('Vault Mutation Applied')
    expect(activityTarget(event)).toBe('A.md')
    expect(relativeActivityTime(event.created_at)).toBe('just now')
  })

  it('adds bounded drag momentum without issuing graph or scan requests', async () => {
    const fetchMock = mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const node = cytoscapeMock.node('harbor')
    emitMockEvent('grab', { target: node })
    node.position({ x: 0, y: 0 })
    emitMockEvent('drag', { target: node })
    node.position({ x: 100, y: 0 })
    emitMockEvent('drag', { target: node })
    emitMockEvent('dragfree', { target: node })
    await new Promise(resolve => setTimeout(resolve, 40))
    expect(fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph/view')).length).toBe(1)
    expect(fetchMock.mock.calls.some(call => String(call[0]).includes('/api/v1/scan'))).toBe(false)
  })

  it('rearranges locally without a global layout', async () => {
    localStorage.removeItem('hlm:tide-atlas:positions:level-2')
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const layouts = cytoscapeMock.layoutCalls.length
    fireEvent.click(screen.getByRole('button', { name: 'Rearrange layout' }))
    expect(cytoscapeMock.layoutCalls).toHaveLength(layouts)
    expect(cytoscapeMock.lockedFor('harbor')).toBe(false)
  })

  it('recomputes positions with a fresh seed when rearranging', async () => {
    localStorage.clear()
    mockAtlas([view(2, null, [cluster('harbor', 'AI'), cluster('second', 'Projects')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toEqual(expect.arrayContaining(['harbor', 'second'])))
    const originalPositions = {
      harbor: cytoscapeMock.positionFor('harbor'),
      second: cytoscapeMock.positionFor('second'),
    }

    cytoscapeMock.node('harbor').position({ x: 480, y: -320 })
    cytoscapeMock.node('second').position({ x: -260, y: 190 })
    fireEvent.click(screen.getByRole('button', { name: 'Rearrange layout' }))

    const rearrangedPositions = {
      harbor: cytoscapeMock.positionFor('harbor'),
      second: cytoscapeMock.positionFor('second'),
    }
    expect((['harbor', 'second'] as const).some(id => {
      const before = originalPositions[id]
      const after = rearrangedPositions[id]
      if (!before || !after) return false
      return before.x !== after.x || before.y !== after.y
    })).toBe(true)
    expect(document.querySelector('[data-layout-revision="1"]')).toBeInTheDocument()
  })

  it.skip('re-anchors contours after Rearrange chart finishes', async () => {
    localStorage.removeItem('hlm:tide-atlas:positions:level-2')
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'Projects/Two.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toEqual(expect.arrayContaining(['one', 'two'])))
    fireEvent.click(screen.getByRole('button', { name: 'Rearrange layout' }))
    cytoscapeMock.node('one').position({ x: 220, y: 140 })
    cytoscapeMock.node('two').position({ x: -80, y: -60 })
    cytoscapeMock.runLastLayoutStop()
    await waitFor(() => expect(screen.getAllByTestId('coastal-island')[0]).toHaveAttribute('transform', expect.stringContaining('translate(220 140)')))
  })

  it('does not fit or clamp zoom during subsequent graph loads', async () => {
    mockAtlas([view(2, null)])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    await new Promise(resolve => setTimeout(resolve, 100))
    expect(cytoscapeMock.fitCalls).toBe(1)
    expect(cytoscapeMock.zoomCalls).toHaveLength(0)
  })

  it('keeps graph data stable until the much-farther LOD boundary', async () => {
    mockAtlas([view(2, null), view(0, null, [cluster('coast', 'AI')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('harbor'))
    const initialLayouts = cytoscapeMock.layoutCalls.length
    setMockZoom(.1)
    emitMockEvent('zoom')
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('coast'))
    expect(cytoscapeMock.layoutCalls).toHaveLength(initialLayouts)
  })
  it.skip('renders one bounded coastal island overlay for each folder group', async () => {
    mockAtlas([view(2, null, [cluster('ai', 'AI/One.md'), cluster('projects', 'Projects/One.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getAllByTestId('coastal-island')).toHaveLength(2))
  })

  it('marks a selected node neighborhood without relayout', async () => {
    mockAtlas([{
      ...view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'AI/Two.md')]),
      edges: [{ id: 'lane', source: 'one', target: 'two', weight: 1 }],
    }])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('one'))
    const layouts = cytoscapeMock.layoutCalls.length
    emitMockEvent('tap', { target: { id: () => 'one' } })
    expect(cytoscapeMock.layoutCalls).toHaveLength(layouts)
    expect(cytoscapeMock.classesFor('one')).toContain('is-neighbor')
    expect(cytoscapeMock.classesFor('two')).toContain('is-neighbor')
    expect(cytoscapeMock.classesFor('lane')).toContain('is-neighbor')
  })

  it('traces only the activity target route and uses a beacon for writes', async () => {
    mockAtlas([{
      ...view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'AI/Two.md'), cluster('three', 'Projects/Three.md')]),
      edges: [{ id: 'lane', source: 'one', target: 'two', weight: 1 }, { id: 'far-lane', source: 'two', target: 'three', weight: 1 }],
    }])
    const rendered = render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('one'))
    rendered.rerender(<TideAtlas events={[{ id: 8, event_type: 'vault.create', created_at: new Date().toISOString(), payload: { path: 'AI/One.md' } }]} />)
    await waitFor(() => expect(cytoscapeMock.classesFor('one')).toEqual(expect.arrayContaining(['activity-glow', 'activity-beacon'])))
    expect(cytoscapeMock.classesFor('two')).toContain('route-trace')
    expect(cytoscapeMock.classesFor('three')).not.toContain('route-trace')
    expect(cytoscapeMock.classesFor('far-lane')).not.toContain('route-trace')
    expect(cytoscapeMock.animations.some(animation => animation.ids.includes('far-lane'))).toBe(false)
  })

  it.skip('projects contour centers with Cytoscape pan and zoom only', async () => {
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'Projects/Two.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getAllByTestId('coastal-island')).toHaveLength(2))
    const before = screen.getAllByTestId('coastal-island').map(island => island.getAttribute('transform'))
    const core = cytoscapeMock.current as unknown as { pan: (point: { x: number; y: number }) => void; zoomValue: number }
    core.pan({ x: 30, y: -12 })
    core.zoomValue = 1.5
    emitMockEvent('pan')
    emitMockEvent('zoom')
    await waitFor(() => {
      const after = screen.getAllByTestId('coastal-island').map(island => island.getAttribute('transform'))
      const coordinates = (transform: string | null): [number, number] => {
        const values = transform?.match(/translate\((-?[\d.]+) (-?[\d.]+)\)/)?.slice(1).map(Number)
        return values && values.length === 2 ? [values[0], values[1]] : [0, 0]
      }
      expect(after.map(coordinates)).toEqual(before.map(coordinates).map(([x, y]) => [x * 1.5 + 30, y * 1.5 - 12]))
      expect(before).not.toEqual(after)
    })
  })

  it.skip('refreshes contour centers from final node positions after initial COSE', async () => {
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md'), cluster('two', 'Projects/Two.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toEqual(expect.arrayContaining(['one', 'two'])))
    cytoscapeMock.node('one').position({ x: 260, y: 180 })
    cytoscapeMock.node('two').position({ x: -140, y: -100 })
    cytoscapeMock.runLastLayoutStop()
    await waitFor(() => expect(screen.getAllByTestId('coastal-island')[0]).toHaveAttribute('transform', expect.stringContaining('translate(260 180)')))
  })

  it.skip('keeps contours deterministic and bounded to three paths', async () => {
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md')])])
    const rendered = render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByTestId('coastal-island')).toBeInTheDocument())
    const island = screen.getByTestId('coastal-island')
    const firstPaths = [...island.querySelectorAll('path')].map(path => path.getAttribute('d'))
    expect(firstPaths).toHaveLength(3)
    rendered.rerender(<TideAtlas events={[]} />)
    expect([...screen.getByTestId('coastal-island').querySelectorAll('path')].map(path => path.getAttribute('d'))).toEqual(firstPaths)
  })

  it.skip('keeps the topography layer pointer-transparent and reduced-motion aware', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('prefers-reduced-motion'), media: query, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    mockAtlas([view(2, null, [cluster('one', 'AI/One.md')])])
    render(<TideAtlas events={[]} />)
    await waitFor(() => expect(screen.getByTestId('atlas-topography')).toHaveAttribute('data-reduced-motion', 'true'))
  })
})
