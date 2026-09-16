import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, App, Settings } from '../main'

type MockResponse = { status?: number; body?: unknown }
type Call = { url: string; method: string; headers?: Record<string, string>; body?: string }

/* Keys are URL prefixes (any method) or 'METHOD /prefix' (one method only).
   The first matching key wins, so list method-specific keys before bare
   prefixes — otherwise a shared '/api/v1/tokens' mock would conflate the
   GET list and the POST create. */
function mockApi(responses: Record<string, MockResponse> = {}) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({ url, method, headers: init?.headers as Record<string, string> | undefined, body: init?.body as string | undefined })
    const entry = Object.entries(responses).find(([key]) => {
      const separator = key.indexOf(' ')
      const matchMethod = separator === -1 ? null : key.slice(0, separator)
      const prefix = separator === -1 ? key : key.slice(separator + 1)
      return (matchMethod === null || matchMethod === method) && url.startsWith(prefix)
    })?.[1]
    const { status = 200, body = {} } = entry ?? {}
    return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body), json: async () => body } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

/* jsdom has no EventSource. Record constructed instances so tests can assert
   on the stream URL (including ?token=) and which streams were closed. */
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners: Record<string, EventListener> = {}
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  addEventListener(type: string, listener: EventListener) { this.listeners[type] = listener }
  removeEventListener(type: string) { delete this.listeners[type] }
  close() { this.closed = true }
}

const streamUrls = () => MockEventSource.instances.map(stream => stream.url)

/* App renders the Graph screen by default; these defaults keep its mount
   effects from crashing or triggering the settings-folder fallback. */
const appResponses = () => ({
  '/api/v1/activity': { body: { events: [] } },
  '/api/v1/settings': { body: { folders: ['AI'] } },
  '/api/v1/graph/view': { body: { level: 2, scope: null, clusters: [], edges: [], generation: 'test' } },
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  MockEventSource.instances = []
})

describe('Settings · API tokens', () => {
  it('presents external token management as an accessible three-zone workspace', async () => {
    mockApi({
      'GET /api/v1/tokens': { body: [{ name: 'agent', rules: [], admin: true, created_at: '2026-01-02T00:00:00Z' }] },
    })
    render(<Settings />)
    expect(screen.getByRole('heading', { name: 'External access' })).toBeInTheDocument()
    expect(screen.getByText(/Web UI uses its own authenticated session/i)).toBeInTheDocument()
    const tokenName = screen.getByRole('textbox', { name: /token name/i })
    expect(tokenName).toHaveAttribute('name', 'token-name')
    expect(tokenName).toHaveAttribute('autocomplete', 'off')
    expect(tokenName).toHaveAttribute('spellcheck', 'false')
    expect(screen.getByRole('checkbox', { name: /admin/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /new external token|create token/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /existing tokens/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /token ready to copy/i })).toBeNull()
    expect(document.querySelector('.tokens-layout')).toHaveAttribute('data-layout', 'single-column')
  })

  it('generates a token: persists the folder draft, POSTs the rules, and reveals the plaintext once', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn(async () => {})
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true })
    const { calls } = mockApi({
      'PUT /api/v1/settings': { status: 200, body: { settings: {} } },
      'POST /api/v1/tokens': { status: 201, body: { token: 'hlm_new', name: 'opencode', rules: [], admin: false, created_at: '2026-01-02T00:00:00Z' } },
      '/api/v1/settings': { status: 200, body: { folders: ['AI'], folder_rules: [{ path: 'AI', access: 'propose-write' }] } },
      '/api/v1/tokens': { status: 200, body: [] },
    })
    render(<Settings />)
    await user.type(screen.getByPlaceholderText(/agent-\d{4}-\d{2}-\d{2}/), 'opencode')
    await user.click(screen.getByRole('button', { name: /generate token/i }))
    await waitFor(() => expect(screen.getByText(/shown only once/)).toBeInTheDocument())
    /* Masked until revealed — the plaintext is never visible by default. */
    expect(screen.queryByText('hlm_new')).toBeNull()
    await user.click(screen.getByRole('button', { name: /^reveal token$/i }))
    expect(screen.getByText('hlm_new')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /^copy token$/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('hlm_new'))
    const put = calls.find(call => call.method === 'PUT' && call.url === '/api/v1/settings')
    const post = calls.find(call => call.method === 'POST' && call.url === '/api/v1/tokens')
    expect(JSON.parse(put?.body ?? '{}')).toEqual({ folder_rules: [{ path: 'AI', access: 'propose-write' }] })
    expect(post?.headers?.['Content-Type']).toBe('application/json')
    expect(JSON.parse(post?.body ?? '{}')).toEqual({ name: 'opencode', rules: [{ path: 'AI', access: 'propose-write' }], admin: false, approve_own_proposals: false })
  })

  it('falls back to a suggested name and sends admin when the checkbox is set', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi({
      'PUT /api/v1/settings': { status: 200, body: {} },
      'POST /api/v1/tokens': { status: 201, body: { token: 'hlm_admin', name: 'agent-x', rules: [], admin: true, created_at: '2026-01-02T00:00:00Z' } },
      '/api/v1/tokens': { status: 200, body: [] },
    })
    render(<Settings />)
    await user.click(screen.getByRole('checkbox', { name: /admin/i }))
    await user.click(screen.getByRole('button', { name: /generate token/i }))
    const suggested = `agent-${new Date().toISOString().slice(0, 10)}`
    await waitFor(() => expect(screen.getByText(/shown only once/)).toBeInTheDocument())
    const post = calls.find(call => call.method === 'POST' && call.url === '/api/v1/tokens')
    expect(JSON.parse(post?.body ?? '{}')).toEqual({ name: suggested, rules: [], admin: true, approve_own_proposals: false })
  })

  it('leaves own-proposal approval unchecked and sends it only after selection', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi({
      'POST /api/v1/tokens': { status: 201, body: { token: 'hlm_proposer', name: 'agent', rules: [], admin: false } },
      '/api/v1/tokens': { body: [] },
    })
    render(<Settings />)
    const checkbox = screen.getByRole('checkbox', { name: /approve own proposals/i })
    expect(checkbox).not.toBeChecked()
    await user.click(checkbox)
    await user.click(screen.getByRole('button', { name: /generate token/i }))
    await waitFor(() => expect(screen.getByText(/shown only once/)).toBeInTheDocument())
    const post = calls.find(call => call.method === 'POST')
    expect(JSON.parse(post?.body ?? '{}').approve_own_proposals).toBe(true)
  })

  it('revokes only after the two-step confirm, with a DELETE by token name', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi({
      'DELETE /api/v1/tokens': { status: 200, body: {} },
      '/api/v1/tokens': { status: 200, body: [{ name: 'opencode', rules: [], admin: false, created_at: '2026-01-01T00:00:00Z' }] },
    })
    render(<Settings />)
    await user.click(await screen.findByRole('button', { name: /revoke token opencode/i }))
    /* Armed: the button asks for confirmation and no DELETE has left the browser. */
    expect(calls.filter(call => call.method === 'DELETE')).toHaveLength(0)
    expect(screen.getByRole('button', { name: /confirm revoke token opencode/i })).toHaveClass('armed')
    await user.click(await screen.findByRole('button', { name: /confirm revoke token opencode/i }))
    const revoke = calls.find(call => call.method === 'DELETE')
    expect(revoke?.url).toBe('/api/v1/tokens/opencode')
    /* The row summarizes its rules instead of a raw scope list. */
    expect(await screen.findByText(/read-only · created/)).toBeInTheDocument()
  })

  it('shows a masked note instead of the generator when the token is not admin (403)', async () => {
    mockApi({ 'GET /api/v1/tokens': { status: 403, body: { detail: 'admin token required' } } })
    render(<Settings />)
    expect(await screen.findByText(/cannot manage external tokens/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /generate token/i })).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

})

describe('Browser session API protection', () => {
  afterEach(() => {
    document.cookie = 'hlm_ui_csrf=; Max-Age=0; path=/'
  })

  it('sends the readable CSRF cookie on unsafe same-origin requests only', async () => {
    const { calls } = mockApi({ '/api/v1/settings': { body: {} } })
    document.cookie = 'hlm_ui_csrf=csrf%2Bvalue; path=/'

    await api('/api/v1/settings', { method: 'PATCH', headers: { 'Content-Type': 'application/json' } })
    await api('/api/v1/settings')

    const mutation = calls.find(call => call.method === 'PATCH')
    const read = calls.find(call => call.method === 'GET')
    expect(mutation?.headers?.['X-HLM-CSRF']).toBe('csrf+value')
    expect(mutation?.headers?.Authorization).toBeUndefined()
    expect(mutation?.url).not.toContain('?token=')
    expect(read?.headers?.['X-HLM-CSRF']).toBeUndefined()
  })
})

describe('App · automatic local UI session', () => {
  it('loads normally without a fragment or browser token and uses cookie authentication', async () => {
    vi.stubGlobal('EventSource', MockEventSource)
    localStorage.setItem('hlm_api_token', 'hlm_external_only')
    const { calls } = mockApi(appResponses())
    render(<App />)
    expect(window.location.hash).toBe('')
    await waitFor(() => expect(calls.some(call => call.url === '/api/v1/status')).toBe(true))
    await waitFor(() => expect(calls.some(call => call.url === '/api/v1/activity?limit=40')).toBe(true))
    await waitFor(() => expect(calls.some(call => call.url.startsWith('/api/v1/graph/view'))).toBe(true))
    await waitFor(() => expect(streamUrls()).toContain('/api/v1/activity/stream'))
    expect(calls.some(call => call.headers?.Authorization)).toBe(false)
    expect(streamUrls().some(url => url.includes('?token='))).toBe(false)
    expect(screen.queryByText(/API token|required|paste/i)).toBeNull()
  })

  it('keeps API-token management available for external integrations', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('EventSource', MockEventSource)
    const { calls } = mockApi({
      'POST /api/v1/tokens': { status: 201, body: { token: 'hlm_new', name: 'external', rules: [], admin: false, created_at: '2026-01-02T00:00:00Z' } },
      '/api/v1/tokens': { body: [] },
      ...appResponses(),
    })
    render(<App />)
    await waitFor(() => expect(screen.getByRole('button', { name: /settings/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /settings/i }))
    await user.type(screen.getByPlaceholderText(/agent-/), 'external')
    await user.click(screen.getByRole('button', { name: /generate token/i }))
    await waitFor(() => expect(screen.getByText(/shown only once/)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /^reveal token$/i }))
    expect(screen.getByText('hlm_new')).toBeInTheDocument()
    const create = calls.find(call => call.method === 'POST' && call.url === '/api/v1/tokens')
    expect(create?.headers?.Authorization).toBeUndefined()
  })

  it('renews the visible cookie session before its idle expiry without a bearer header', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockEventSource)
    const { calls } = mockApi(appResponses())
    render(<App />)

    await act(async () => { await vi.advanceTimersByTimeAsync(25 * 60 * 1000) })

    const statusCalls = calls.filter(call => call.url === '/api/v1/status')
    expect(statusCalls).toHaveLength(2)
    expect(statusCalls[1]?.headers?.Authorization).toBeUndefined()
    vi.useRealTimers()
  })

  it('stops renewal while hidden and validates immediately when visible', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockEventSource)
    const { calls } = mockApi(appResponses())
    render(<App />)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await act(async () => { await vi.advanceTimersByTimeAsync(30 * 60 * 1000) })
    expect(calls.filter(call => call.url === '/api/v1/status')).toHaveLength(1)

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(calls.filter(call => call.url === '/api/v1/status')).toHaveLength(2)
    vi.useRealTimers()
  })

  it('remembers Approvals for return when cookie authentication receives 401', async () => {
    vi.stubGlobal('EventSource', MockEventSource)
    const { fetchMock } = mockApi(appResponses())
    const view = render(<App />)
    await userEvent.setup().click(screen.getByRole('button', { name: /approvals/i }))
    fetchMock.mockResolvedValueOnce({ ok: false, status: 401, statusText: 'Unauthorized', text: async () => JSON.stringify({ detail: 'expired' }) } as Response)
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await api('/api/v1/status').catch(() => undefined)
    expect(sessionStorage.getItem('hlm_ui_return_screen')).toBe('approvals')
    view.unmount()
    render(<App />)
    expect(screen.getByRole('heading', { name: 'Approvals', level: 2 })).toBeInTheDocument()
  })

  it('shows a 403 renewal error without leaving the visible Approvals view', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockEventSource)
    sessionStorage.setItem('hlm_ui_return_screen', 'approvals')
    const { fetchMock } = mockApi()
    let statusCalls = 0
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const forbidden = String(input) === '/api/v1/status' && ++statusCalls > 1
      const body = forbidden ? { detail: 'authorization required' } : {}
      return { ok: !forbidden, status: forbidden ? 403 : 200, statusText: forbidden ? 'Forbidden' : 'OK', text: async () => JSON.stringify(body), json: async () => body } as Response
    })
    render(<App />)

    await act(async () => { await vi.advanceTimersByTimeAsync(25 * 60 * 1000) })

    expect(screen.getByRole('heading', { name: 'Approvals', level: 2 })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('authorization required')
    expect(sessionStorage.getItem('hlm_ui_return_screen')).toBeNull()
  })

  it('does not redirect on 403 authorization errors', async () => {
    vi.stubGlobal('EventSource', MockEventSource)
    const { fetchMock } = mockApi(appResponses())
    render(<App />)
    fetchMock.mockResolvedValueOnce({ ok: false, status: 403, statusText: 'Forbidden', text: async () => JSON.stringify({ detail: 'forbidden' }) } as Response)
    await api('/api/v1/status').catch(() => undefined)
    expect(sessionStorage.getItem('hlm_ui_return_screen')).toBeNull()
  })
})
