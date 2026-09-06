import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Graph, PULSE_MS } from '../main'
import { cytoscapeMock } from '../test/cytoscape-mock'

const STATUS_BODY = {
  indexed_notes: 2,
  scan_runs: 1,
  diagnostics: 0,
  broken_links: 0,
  ambiguous_links: 0,
  last_scan_status: 'completed',
  last_scan_completed_at: '2026-08-18T00:00:00Z',
  effective_read_scope: '.',
}

/* Stub fetch; /api/v1/graph serves the given snapshot (or a queue of snapshots
   so sequential calls change the response). */
function mockGraph(responses: unknown | unknown[]) {
  const queue = Array.isArray(responses) ? [...responses] : [responses]
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/graph')) {
      const body = queue.length > 1 ? queue.shift() : queue[0]
      return { ok: true, json: async () => body } as Response
    }
    if (url.includes('/api/v1/status')) {
      return { ok: true, json: async () => STATUS_BODY } as Response
    }
    return { ok: true, json: async () => ({ events: [] }) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/* Like mockGraph, but the first /api/v1/graph response stays pending until
   resolve() is called — used to deliver activity events before the core exists. */
function mockGraphDeferred(body: unknown) {
  let resolveBody: (value: unknown) => void = () => undefined
  const pending = new Promise(resolve => { resolveBody = resolve })
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/graph')) {
      await pending
      return { ok: true, json: async () => body } as Response
    }
    if (url.includes('/api/v1/status')) {
      return { ok: true, json: async () => STATUS_BODY } as Response
    }
    return { ok: true, json: async () => ({ events: [] }) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return { resolve: () => resolveBody(body), fetchMock }
}

/* Each /api/v1/graph call gets its own gate and body, so responses can be
   released in any order (out-of-order testing). */
function mockGraphDeferredQueue(bodies: unknown[]) {
  let call = 0
  const gates = new Map<number, () => void>()
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/graph')) {
      const idx = call++
      await new Promise<void>(resolve => gates.set(idx, resolve))
      return { ok: true, json: async () => bodies[idx] } as Response
    }
    if (url.includes('/api/v1/status')) {
      return { ok: true, json: async () => STATUS_BODY } as Response
    }
    return { ok: true, json: async () => ({ events: [] }) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return { release: (idx: number) => gates.get(idx)?.(), fetchMock }
}

const graphCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph')).length

const event = (id: number, event_type: string, payload: Record<string, unknown>) => ({
  id,
  event_type,
  created_at: '2026-08-18T00:00:00Z',
  payload,
})

const QUERY_EVENT = event(1, 'query', {
  query: 'test',
  graph_refs: ['A.md'],
  graph_path: [{ source: 'A.md', target: 'B.md', edge_type: 'wikilink', traversal_direction: 'forward' }],
})

const mount = (events: unknown[]) => render(<Graph events={events as never[]} />)

describe('Graph', () => {
  it('renders isolated API nodes without synthetic edges', async () => {
    mockGraph({ nodes: [{ path: 'Isolated.md', title: 'Isolated', isolated: true }], edges: [], generation: 1 })
    render(<Graph events={[]} />)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('Isolated.md'))
    expect(cytoscapeMock.edgeIds()).toEqual([])
  })

  it('maps API nodes and edges to cytoscape without generated topology', async () => {
    mockGraph({
      nodes: [
        { path: 'A.md', title: 'A', isolated: false },
        { path: 'B.md', title: 'B', isolated: false },
      ],
      edges: [{ id: 'edge-1', source: 'A.md', target: 'B.md', edge_type: 'wikilink', explicit: true }],
      generation: 'gen-1',
    })
    render(<Graph events={[]} />)
    await waitFor(() => expect([...cytoscapeMock.nodeIds()].sort()).toEqual(['A.md', 'B.md']))
    expect(cytoscapeMock.edgeIds()).toEqual(['edge-1'])
  })

  it('uses bounded random placement for connected notes', async () => {
    mockGraph({ nodes: [{ path: 'A.md', title: 'A', isolated: false }], edges: [], generation: 1 })
    render(<Graph events={[]} />)

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))
    expect((cytoscapeMock as any).options?.layout).toMatchObject({
      name: 'preset',
      padding: 92,
    })
  })

  it('refreshes the graph snapshot after a scan event', async () => {
    const fetchMock = mockGraph([
      { nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' },
      {
        nodes: [
          { path: 'A.md', title: 'A', isolated: true },
          { path: 'C.md', title: 'C', isolated: true },
        ],
        edges: [],
        generation: 'gen-2',
      },
    ])
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[event(1, 'scan', { files_indexed: 2, graph_refs: ['A.md', 'C.md'] })]} />)

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('C.md'))
    expect(graphCalls(fetchMock)).toBe(2)
  })

  it('refreshes the graph snapshot after an applied write event', async () => {
    const fetchMock = mockGraph([
      { nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' },
      {
        nodes: [
          { path: 'A.md', title: 'A', isolated: true },
          { path: 'W.md', title: 'W', isolated: false },
        ],
        edges: [{ id: 'edge-w', source: 'A.md', target: 'W.md', edge_type: 'wikilink', explicit: false }],
        generation: 'gen-2',
      },
    ])
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[event(1, 'vault.mutation.applied', { path: 'W.md', graph_refs: ['W.md'] })]} />)

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('W.md'))
    expect(cytoscapeMock.edgeIds()).toContain('edge-w')
    expect(graphCalls(fetchMock)).toBe(2)
  })

  it('applies query activity classes without recreating cytoscape', async () => {
    mockGraph({
      nodes: [
        { path: 'A.md', title: 'A', isolated: false },
        { path: 'B.md', title: 'B', isolated: false },
      ],
      edges: [{ id: 'edge-1', source: 'A.md', target: 'B.md', edge_type: 'wikilink', explicit: true }],
      generation: 'gen-1',
    })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[QUERY_EVENT]} />)

    await waitFor(() => expect(cytoscapeMock.classesFor('B.md')).toContain('query-active'))
    expect(cytoscapeMock.classesFor('edge-1')).toContain('path-active')
    expect(cytoscapeMock.classesFor('A.md')).not.toContain('query-active')
    expect(cytoscapeMock.createCalls).toBe(1)
  })

  it('applies write activity classes without recreating cytoscape', async () => {
    mockGraph({
      nodes: [{ path: 'A.md', title: 'A', isolated: true }],
      edges: [],
      generation: 'gen-1',
    })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    /* Same generation on refetch, so the applied-write refresh must not recreate the core. */
    view.rerender(<Graph events={[event(1, 'vault.mutation.applied', { path: 'A.md', graph_refs: ['A.md'] })]} />)

    await waitFor(() => expect(cytoscapeMock.classesFor('A.md')).toContain('write-active'))
    expect(cytoscapeMock.createCalls).toBe(1)
  })

  it('queues activity events that arrive before the core and replays them on init', async () => {
    const { resolve } = mockGraphDeferred({
      nodes: [
        { path: 'A.md', title: 'A', isolated: false },
        { path: 'B.md', title: 'B', isolated: false },
      ],
      edges: [{ id: 'edge-1', source: 'A.md', target: 'B.md', edge_type: 'wikilink', explicit: true }],
      generation: 'gen-1',
    })
    const view: RenderResult = mount([])
    view.rerender(<Graph events={[QUERY_EVENT]} />)

    /* The snapshot is still pending, so no core exists and the event must be queued. */
    expect(cytoscapeMock.createCalls).toBe(0)

    resolve()

    await waitFor(() => expect(cytoscapeMock.classesFor('B.md')).toContain('query-active'))
    expect(cytoscapeMock.classesFor('edge-1')).toContain('path-active')
    expect(cytoscapeMock.createCalls).toBe(1)
  })

  it('replays scan activity after a snapshot refresh so new nodes stay highlighted', async () => {
    const fetchMock = mockGraph([
      { nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' },
      {
        nodes: [
          { path: 'A.md', title: 'A', isolated: false },
          { path: 'C.md', title: 'C', isolated: false },
        ],
        edges: [{ id: 'edge-c', source: 'A.md', target: 'C.md', edge_type: 'wikilink', explicit: false }],
        generation: 'gen-2',
      },
    ])
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[event(1, 'scan', { files_indexed: 2, graph_refs: ['A.md', 'C.md'] })]} />)

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('C.md'))
    /* The rebuild replaces the core, so the terminal scan highlight must be
       replayed onto the node that only exists after the refresh. */
    await waitFor(() => expect(cytoscapeMock.classesFor('C.md')).toContain('scan-active'))
    expect(cytoscapeMock.classesFor('A.md')).not.toContain('scan-active')
    expect(cytoscapeMock.classesFor('edge-c')).not.toContain('path-active')
    expect(graphCalls(fetchMock)).toBe(2)
  })

  it('surfaces an initial load failure with an explicit retry that recovers', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/graph')) {
        const calls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph')).length
        if (calls === 1) return { ok: false, statusText: '502', text: async () => 'graph unavailable' } as Response
        return {
          ok: true,
          json: async () => ({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 1 }),
        } as Response
      }
      if (url.includes('/api/v1/status')) {
        return { ok: true, json: async () => STATUS_BODY } as Response
      }
      return { ok: true, json: async () => ({ events: [] }) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    mount([])

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('graph unavailable'))
    const retry = screen.getByRole('button', { name: /retry/i })
    await userEvent.click(retry)

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))
    expect(graphCalls(fetchMock)).toBe(2)
  })

  it('destroys the cytoscape core on unmount', async () => {
    mockGraph({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 1 })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    expect(cytoscapeMock.destroyCalls).toBe(0)
    view.unmount()
    expect(cytoscapeMock.destroyCalls).toBe(1)
  })

  it('runs a bounded flow animation for active elements', async () => {
    mockGraph({
      nodes: [
        { path: 'A.md', title: 'A', isolated: false },
        { path: 'B.md', title: 'B', isolated: false },
      ],
      edges: [{ id: 'edge-1', source: 'A.md', target: 'B.md', edge_type: 'wikilink', explicit: true }],
      generation: 'gen-1',
    })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[QUERY_EVENT]} />)

    await waitFor(() => expect(cytoscapeMock.classesFor('B.md')).toContain('query-active'))
    /* The route advances edge-by-edge and only then highlights the terminal. */
    expect(cytoscapeMock.animations.length).toBe(2)
    expect(cytoscapeMock.animations.every(a => a.duration === PULSE_MS)).toBe(true)
    expect(cytoscapeMock.animations.some(a => a.ids.includes('B.md'))).toBe(true)
    expect(cytoscapeMock.animations.some(a => a.ids.includes('edge-1'))).toBe(true)
  })

  it('ignores out-of-order graph fetch responses so only the latest wins', async () => {
    const { release } = mockGraphDeferredQueue([
      { nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' },
      {
        nodes: [
          { path: 'A.md', title: 'A', isolated: false },
          { path: 'C.md', title: 'C', isolated: false },
        ],
        edges: [{ id: 'edge-c', source: 'A.md', target: 'C.md', edge_type: 'wikilink', explicit: false }],
        generation: 'gen-2',
      },
    ])
    const view: RenderResult = mount([]) /* call 0 (initial) stays pending */
    view.rerender(<Graph events={[event(1, 'scan', { files_indexed: 2, graph_refs: ['A.md', 'C.md'] })]} />) /* call 1 (refresh) */

    expect(cytoscapeMock.createCalls).toBe(0)

    /* The refresh (newer) response arrives first and must win. */
    release(1)
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('C.md'))

    /* The initial (older) response arrives late and must be ignored. */
    release(0)
    await new Promise(r => setTimeout(r, 25))
    expect([...cytoscapeMock.nodeIds()].sort()).toEqual(['A.md', 'C.md'])
    expect(cytoscapeMock.createCalls).toBe(1)
  })

  it('shows a visible retryable error when a latest refresh fails before any snapshot commits', async () => {
    let releaseInitial: () => void = () => undefined
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/graph')) {
        const calls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph')).length
        if (calls === 1) {
          /* Initial request: held pending until releaseInitial(). */
          await new Promise<void>(resolve => { releaseInitial = resolve })
          return { ok: true, json: async () => ({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' }) } as Response
        }
        if (calls === 2) return { ok: false, statusText: '503', text: async () => 'upstream down' } as Response
        return { ok: true, json: async () => ({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' }) } as Response
      }
      if (url.includes('/api/v1/status')) return { ok: true, json: async () => STATUS_BODY } as Response
      return { ok: true, json: async () => ({ events: [] }) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    const view: RenderResult = mount([]) /* call 1 (initial) stays pending */

    /* A scan event triggers a refresh (call 2, now the latest) while the initial is still pending. */
    view.rerender(<Graph events={[event(1, 'scan', { files_indexed: 1, graph_refs: ['A.md'] })]} />)

    /* The latest request failed and no snapshot was ever committed, so the user must
       see the error with a retry — not a stuck "indexing notes…" loader. */
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/upstream down/))
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()

    /* The older initial response arrives late and must not clobber the error state
       (latest-request sequencing) or build a core from a superseded snapshot. */
    releaseInitial()
    await new Promise(r => setTimeout(r, 25))
    expect(screen.getByRole('alert').textContent).toMatch(/upstream down/)
    expect(cytoscapeMock.createCalls).toBe(0)

    /* Retry recovers. */
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(graphCalls(fetchMock)).toBe(3)
  })

  it('marks a failed background refresh as stale and recovers on retry', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/graph')) {
        const calls = fetchMock.mock.calls.filter(call => String(call[0]).includes('/api/v1/graph')).length
        if (calls === 1) {
          return { ok: true, json: async () => ({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' }) } as Response
        }
        if (calls === 2) return { ok: false, statusText: '503', text: async () => 'upstream stale' } as Response
        return {
          ok: true,
          json: async () => ({
            nodes: [
              { path: 'A.md', title: 'A', isolated: false },
              { path: 'C.md', title: 'C', isolated: false },
            ],
            edges: [{ id: 'edge-c', source: 'A.md', target: 'C.md', edge_type: 'wikilink', explicit: false }],
            generation: 'gen-2',
          }),
        } as Response
      }
      if (url.includes('/api/v1/status')) return { ok: true, json: async () => STATUS_BODY } as Response
      return { ok: true, json: async () => ({ events: [] }) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(<Graph events={[event(1, 'scan', { files_indexed: 2, graph_refs: ['A.md', 'C.md'] })]} />)

    /* The failed refresh must not blank the graph — it flags stale data. */
    await waitFor(() => expect(screen.getByText('live update stale')).toBeTruthy())
    expect(cytoscapeMock.nodeIds()).toContain('A.md')

    await userEvent.click(screen.getByRole('button', { name: /retry/i }))

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('C.md'))
    expect(screen.queryByText('live update stale')).toBeNull()
    expect(graphCalls(fetchMock)).toBe(3)
  })

  it('surfaces a rescan polling timeout as a visible retryable error', async () => {
    mockGraph({ nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    vi.useFakeTimers()
    try {
      fireEvent.click(screen.getByRole('button', { name: /^rescan$/i }))
      /* 60 polls × 1s, and the status timestamp never advances. The act()
         flush lets React commit the state updates the finished poller makes. */
      await act(async () => {
        await vi.advanceTimersByTimeAsync(61_000)
      })

      expect(screen.getByRole('alert').textContent).toMatch(/did not complete in time/i)
      const rescan = screen.getByRole('button', { name: /^rescan$/i }) as HTMLButtonElement
      expect(rescan.disabled).toBe(false) /* still retryable */
    } finally {
      vi.useRealTimers()
    }
  })

  it('supports keyboard selection of graph nodes', async () => {
    mockGraph({
      nodes: [
        { path: 'A.md', title: 'Alpha', isolated: false },
        { path: 'B.md', title: 'Beta', isolated: false },
      ],
      edges: [{ id: 'edge-1', source: 'A.md', target: 'B.md', edge_type: 'wikilink', explicit: true }],
      generation: 'gen-1',
    })
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    const canvas = view.container.querySelector('.cytoscape')
    expect(canvas).not.toBeNull()

    fireEvent.keyDown(canvas as Element, { key: 'ArrowRight' })
    const details = screen.getByRole('region', { name: 'Selected note details' })
    expect(details.textContent).toContain('Alpha')

    fireEvent.keyDown(canvas as Element, { key: 'ArrowRight' })
    expect(details.textContent).toContain('Beta')

    fireEvent.keyDown(canvas as Element, { key: 'Escape' })
    expect(screen.queryByRole('region', { name: 'Selected note details' })).toBeNull()
  })

  it('replays the write flow after a generation refresh', async () => {
    const fetchMock = mockGraph([
      { nodes: [{ path: 'A.md', title: 'A', isolated: true }], edges: [], generation: 'gen-1' },
      {
        nodes: [
          { path: 'A.md', title: 'A', isolated: false },
          { path: 'W.md', title: 'W', isolated: false },
        ],
        edges: [{ id: 'edge-w', source: 'A.md', target: 'W.md', edge_type: 'wikilink', explicit: false }],
        generation: 'gen-2',
      },
    ])
    const view: RenderResult = mount([])
    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('A.md'))

    view.rerender(
      <Graph events={[event(1, 'vault.mutation.applied', { path: 'W.md', graph_refs: ['A.md', 'W.md'] })]} />,
    )

    await waitFor(() => expect(cytoscapeMock.nodeIds()).toContain('W.md'))
    /* The generation change rebuilds the core, so the write terminal must be
       replayed onto the new node after the refresh. */
    await waitFor(() => expect(cytoscapeMock.classesFor('W.md')).toContain('write-active'))
    expect(cytoscapeMock.classesFor('A.md')).not.toContain('write-active')
    expect(cytoscapeMock.createCalls).toBe(2)
    expect(graphCalls(fetchMock)).toBe(2)
  })
})
