import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Settings } from '../main'
import type { WriteProposal } from '../main'

const SETTINGS_BODY = {
  vault_path: '/tmp/hlm-test-vault',
  index_root: '/tmp/hlm-test-vault',
  config_path: '/tmp/hlm-test-config.json',
  memory_capacity: 50,
  embedding_model: null,
  folder_rules: [{ path: 'AI', access: 'propose-write' }],
  folders: ['AI', 'Projects', 'Projects/Client', 'Projects/Private'],
}

const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

const pendingProposal: WriteProposal = {
  id: 7,
  path: 'AI/new.md',
  content: '# New note\n\nSome body text',
  operation: 'create',
  status: 'pending',
  rule_access: 'propose-write',
  requested_at: minutesAgo(4),
  resolved_at: null,
  failure_reason: null,
}

type FetchCall = { url: string; method: string }
type Failure = { status: number; text: string }

/* This workspace has no @types/node: a non-literal import specifier skips tsc's
   module resolution (intentionally untyped; vitest runs on Node). */
declare const process: { cwd(): string }
const fsSpecifier: string = 'node:fs'
const { readFileSync } = (await import(fsSpecifier)) as { readFileSync: (path: string, encoding: 'utf8') => string }

/* vitest runs with the package root as cwd; read the real stylesheet (a ?raw
   import is stubbed to '' because vitest disables CSS processing by default). */
const css = readFileSync(`${process.cwd()}/src/styles.css`, 'utf8')

/* Queue of GET /api/v1/writes bodies; the last entry repeats for later calls. */
function mockWrites(lists: unknown[], options: { getWrites?: Failure; postWrites?: Failure } = {}) {
  let listCalls = 0
  const calls: FetchCall[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({ url, method })
    const fail = (f: Failure) => ({ ok: f.status >= 200 && f.status < 300, status: f.status, text: async () => f.text } as Response)
    const postMatch = url.match(/\/api\/v1\/writes\/(\d+)\/(approve|reject)$/)
    if (postMatch) {
      if (options.postWrites) return fail(options.postWrites)
      /* Mirror the backend: a successful POST returns the resolved proposal. */
      const id = Number(postMatch[1])
      const status = postMatch[2] === 'approve' ? 'applied' : 'rejected'
      const source = (lists[0] as { proposals?: WriteProposal[] })?.proposals?.find(w => w.id === id) ?? ({} as WriteProposal)
      return { ok: true, json: async () => ({ ...source, status, resolved_at: new Date().toISOString() }) } as Response
    }
    if (url === '/api/v1/writes' && method === 'POST') {
      if (options.postWrites) return fail(options.postWrites)
      const body = JSON.parse(String(init?.body ?? '{}')) as { path?: string; content?: string; operation?: string }
      const response = (lists[1] as { proposals?: WriteProposal[] })?.proposals?.[0]
      return { ok: true, json: async () => response ?? { ...pendingProposal, path: body.path ?? '', content: body.content ?? '', operation: body.operation ?? '' } } as Response
    }
    if (options.getWrites && method === 'GET' && /^\/api\/v1\/writes$/.test(url)) return fail(options.getWrites)
    if (url.includes('/api/v1/settings')) return { ok: true, json: async () => SETTINGS_BODY } as Response
    if (method === 'GET' && /^\/api\/v1\/writes$/.test(url)) {
      const body = listCalls < lists.length ? lists[listCalls++] : lists[lists.length - 1]
      return { ok: true, json: async () => body } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

const writesGets = (calls: FetchCall[]) => calls.filter(c => c.url === '/api/v1/writes' && c.method === 'GET')

describe('Settings · vault writes', () => {
  it('shows discovered folders and selects a folder to configure', async () => {
    mockWrites([{ proposals: [] }])
    render(<Settings />)
    const user = userEvent.setup()

    await user.click(await screen.findByLabelText('Expand Projects'))
    await user.click(screen.getByText('Client').closest('button') as HTMLButtonElement)

    expect(screen.getByLabelText('Permission for Projects/Client')).toBeTruthy()
  })

  it('renders pending proposals with path, operation, rule access, and a content preview', async () => {
    mockWrites([{ proposals: [pendingProposal] }])
    render(<Settings />)
    const card = await screen.findByRole('article', { name: 'write proposal for AI/new.md' })
    expect(within(card).getByText('create')).toBeTruthy()
    expect(within(card).getByText('propose-write')).toBeTruthy()
    expect(within(card).getByText(/Some body text/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Approve AI/new.md' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reject AI/new.md' })).toBeTruthy()
  })

  it('approves a pending proposal, then refreshes pending and audit lists', async () => {
    const applied: WriteProposal = { ...pendingProposal, status: 'applied', resolved_at: minutesAgo(1) }
    const { calls } = mockWrites([{ proposals: [pendingProposal] }, { proposals: [applied] }])
    render(<Settings />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Approve AI/new.md' }))
    await waitFor(() => expect(calls.some(c => c.url === '/api/v1/writes/7/approve' && c.method === 'POST')).toBe(true))
    await screen.findByText('applied')
    expect(screen.queryByRole('button', { name: 'Approve AI/new.md' })).toBeNull()
    expect(screen.getByText(/no pending proposals/)).toBeTruthy()
    await waitFor(() => expect(writesGets(calls).length).toBe(2))
  })

  it('rejects a pending proposal, then refreshes pending and audit lists', async () => {
    const rejected: WriteProposal = { ...pendingProposal, status: 'rejected', resolved_at: minutesAgo(1) }
    const { calls } = mockWrites([{ proposals: [pendingProposal] }, { proposals: [rejected] }])
    render(<Settings />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Reject AI/new.md' }))
    await waitFor(() => expect(calls.some(c => c.url === '/api/v1/writes/7/reject' && c.method === 'POST')).toBe(true))
    await screen.findByText('rejected')
    expect(screen.queryByRole('button', { name: 'Reject AI/new.md' })).toBeNull()
    await waitFor(() => expect(writesGets(calls).length).toBe(2))
  })

  it('renders recent audit records with status and failure reason', async () => {
    mockWrites([{
      proposals: [
        { ...pendingProposal, id: 9, path: 'AI/kept.md', status: 'applied', resolved_at: minutesAgo(30) },
        { ...pendingProposal, id: 8, path: 'AI/failed.md', status: 'failed', failure_reason: 'vault path disappeared', resolved_at: minutesAgo(12) },
      ],
    }])
    render(<Settings />)
    expect(await screen.findByText('AI/kept.md')).toBeTruthy()
    expect(screen.getByText('AI/failed.md')).toBeTruthy()
    expect(screen.getByText('applied')).toBeTruthy()
    expect(screen.getByText('failed')).toBeTruthy()
    expect(screen.getByText('vault path disappeared')).toBeTruthy()
  })

  it('uses neutral audit wording when a folder proposal is rejected', async () => {
    mockWrites([{
      proposals: [{ ...pendingProposal, id: 10, path: 'AI/Rejected', content: '', operation: 'mkdir', status: 'rejected', resolved_at: minutesAgo(3) }],
    }])
    render(<Settings />)

    expect(await screen.findByText('AI/Rejected')).toBeTruthy()
    expect(screen.getByText('folder proposal')).toBeTruthy()
    expect(screen.queryByText('folder created')).toBeNull()
  })

  it('shows empty states when there are no writes', async () => {
    mockWrites([{ proposals: [] }])
    render(<Settings />)
    expect(await screen.findByText(/no pending proposals/)).toBeTruthy()
    expect(screen.getByText(/no writes yet/)).toBeTruthy()
  })

  it('submits an accessible folder proposal and renders its pending state', async () => {
    const { calls, fetchMock } = mockWrites([{ proposals: [] }, { proposals: [{
      ...pendingProposal, path: 'AI/Inbox', content: '', operation: 'mkdir',
    }] }])
    render(<Settings />)
    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('New folder path'), 'AI/Inbox')
    await user.click(screen.getByRole('button', { name: 'Create folder' }))

    await waitFor(() => expect(calls).toContainEqual({ url: '/api/v1/writes', method: 'POST' }))
    expect(JSON.parse(String(fetchMock.mock.calls.find(([, init]) => init?.method === 'POST' && String(init.body).includes('mkdir'))?.[1]?.body))).toEqual({ path: 'AI/Inbox', content: '', operation: 'mkdir' })
    expect(await screen.findByText('mkdir')).toBeTruthy()
  })

  it('exposes a folder proposal error and re-enables the form after a forbidden response', async () => {
    mockWrites([{ proposals: [] }], { postWrites: { status: 403, text: 'folder proposals are not allowed' } })
    render(<Settings />)
    const user = userEvent.setup()
    const input = await screen.findByLabelText('New folder path')
    await user.type(input, 'AI/Private')
    await user.click(screen.getByRole('button', { name: 'Create folder' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('folder proposals are not allowed')
    expect(screen.getByRole('button', { name: 'Create folder' })).toBeEnabled()
  })

  it('shows an error state with retry when the writes API fails', async () => {
    const { calls } = mockWrites([{ proposals: [pendingProposal] }], { getWrites: { status: 500, text: 'vault is unavailable' } })
    render(<Settings />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('vault is unavailable')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'retry' }))
    await waitFor(() => expect(writesGets(calls).length).toBe(2))
  })

  it('surfaces a per-proposal action error and re-enables the controls', async () => {
    mockWrites([{ proposals: [pendingProposal] }], { postWrites: { status: 400, text: 'proposal 7 is already applied, not pending' } })
    render(<Settings />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Approve AI/new.md' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('already applied')
    const approve = screen.getByRole('button', { name: 'Approve AI/new.md' }) as HTMLButtonElement
    const reject = screen.getByRole('button', { name: 'Reject AI/new.md' }) as HTMLButtonElement
    expect(approve.disabled).toBe(false)
    expect(reject.disabled).toBe(false)
  })

  it('triggers approve from the keyboard', async () => {
    const { calls } = mockWrites([{ proposals: [pendingProposal] }])
    render(<Settings />)
    const user = userEvent.setup()
    const approve = await screen.findByRole('button', { name: 'Approve AI/new.md' })
    approve.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(calls.some(c => c.url === '/api/v1/writes/7/approve' && c.method === 'POST')).toBe(true))
  })

  it('keeps long content previews safe and truncated', async () => {
    const long = { ...pendingProposal, content: 'word '.repeat(120) }
    mockWrites([{ proposals: [long] }])
    const { container } = render(<Settings />)
    await waitFor(() => {
      const preview = container.querySelector('.write-preview')
      expect(preview?.textContent).toContain('…')
    })
    const preview = container.querySelector('.write-preview')
    const text = preview?.textContent ?? ''
    expect(text).not.toBe(long.content)
    expect(text.length).toBeLessThanOrEqual(162)
    /* The tooltip reuses the same capped preview — no raw content exposure. */
    expect(preview?.getAttribute('title')).toBe(text)
  })

  it('moves the proposal to the audit before the post-success refresh settles', async () => {
    const applied: WriteProposal = { ...pendingProposal, status: 'applied', resolved_at: minutesAgo(1) }
    let refreshGets = 0
    let resolveRefresh: (value: Response) => void = () => undefined
    const calls: FetchCall[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url, method })
      if (url.includes('/api/v1/settings')) return { ok: true, json: async () => SETTINGS_BODY } as Response
      if (/\/api\/v1\/writes\/\d+\/approve$/.test(url)) {
        return { ok: true, json: async () => applied } as Response
      }
      if (method === 'GET' && /^\/api\/v1\/writes$/.test(url)) {
        refreshGets += 1
        if (refreshGets === 1) return { ok: true, json: async () => ({ proposals: [pendingProposal] }) } as Response
        return new Promise<Response>(resolve => { resolveRefresh = resolve })
      }
      return { ok: true, json: async () => ({}) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<Settings />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Approve AI/new.md' }))
    await waitFor(() => expect(calls.some(c => c.url === '/api/v1/writes/7/approve' && c.method === 'POST')).toBe(true))
    await waitFor(() => expect(refreshGets).toBe(2))
    /* While the refresh is in flight the POST response must already have moved the
       proposal into the audit: no stale action, resolved state clear. */
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve AI/new.md' })).toBeNull())
    expect(await screen.findByText('applied')).toBeTruthy()
    resolveRefresh({ ok: true, json: async () => ({ proposals: [applied] }) } as Response)
    /* The settled refresh must not duplicate the audit entry. */
    await waitFor(() => expect(screen.getAllByText('applied').length).toBe(1))
  })

  it('keeps a resolved proposal non-actionable when the post-success refresh fails', async () => {
    const applied: WriteProposal = { ...pendingProposal, status: 'applied', resolved_at: minutesAgo(1) }
    let refreshGets = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.includes('/api/v1/settings')) return { ok: true, json: async () => SETTINGS_BODY } as Response
      if (/\/api\/v1\/writes\/\d+\/approve$/.test(url)) {
        return { ok: true, json: async () => applied } as Response
      }
      if (method === 'GET' && /^\/api\/v1\/writes$/.test(url)) {
        refreshGets += 1
        if (refreshGets === 1) return { ok: true, json: async () => ({ proposals: [pendingProposal] }) } as Response
        if (refreshGets === 2) return { ok: false, status: 500, text: async () => 'vault is unavailable' } as Response
        /* The retry lands on a stale snapshot that still lists the proposal as pending. */
        return { ok: true, json: async () => ({ proposals: [pendingProposal] }) } as Response
      }
      return { ok: true, json: async () => ({}) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<Settings />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Approve AI/new.md' }))
    const stale = await screen.findByText(/live update failed/)
    const bar = stale.closest('.writes-stale')
    expect(bar).toBeTruthy()
    /* The POST response moved the proposal into the audit: no stale action can
       fire again, and the resolved state is clear. */
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Approve AI/new.md' })).toBeNull())
    expect(screen.queryByRole('button', { name: 'Reject AI/new.md' })).toBeNull()
    expect(screen.getByText('applied')).toBeTruthy()
    /* The stale note's retry re-fetches the list… */
    await user.click(within(bar as HTMLElement).getByRole('button', { name: 'retry' }))
    await waitFor(() => expect(refreshGets).toBe(3))
    /* …and a stale snapshot must not reintroduce the pending card or duplicate UI. */
    await waitFor(() => expect(screen.queryByText(/live update failed/)).toBeNull())
    expect(screen.queryByRole('button', { name: 'Approve AI/new.md' })).toBeNull()
    expect(screen.getAllByText('applied').length).toBe(1)
  })

  it('renders the proposal queue with footer metadata and a separate activity region', async () => {
    mockWrites([{ proposals: [pendingProposal] }])
    const { container } = render(<Settings />)
    await screen.findByRole('article', { name: 'write proposal for AI/new.md' })
    const layout = container.querySelector('.writes-layout')
    expect(layout).toBeTruthy()
    expect(layout?.querySelectorAll('.writes-panel').length).toBe(1)
    expect(container.querySelector('.audit-region')).toBeTruthy()
    const footer = container.querySelector('.write-card-foot')
    expect(footer?.querySelector('time')).toBeTruthy()
    expect(footer?.querySelectorAll('.write-actions button').length).toBe(2)
  })

  it('arranges tokens and proposals beside each other with a ten-event full-width audit', async () => {
    const audit = Array.from({ length: 11 }, (_, index) => ({
      ...pendingProposal,
      id: 100 + index,
      path: `AI/audit-${index}.md`,
      status: 'applied' as const,
      resolved_at: minutesAgo(index + 1),
    }))
    mockWrites([{ proposals: audit }])
    const { container } = render(<Settings />)
    await screen.findByText('AI/audit-0.md')
    const workspace = container.querySelector('.settings-workspace-layout')
    expect(workspace).toHaveAttribute('data-layout', 'quiet-split')
    expect(workspace?.querySelector('.tokens-section')).toBeTruthy()
    expect(workspace?.querySelector('.write-proposals-region')).toBeTruthy()
    expect(workspace?.querySelector('.audit-region')).toBeTruthy()
    expect(workspace?.querySelector('.audit-region')).not.toHaveClass('panel')
    expect(workspace?.querySelectorAll('.audit-row')).toHaveLength(10)
  })

  it('collapses the writes layout at ≤900px and stacks card actions at narrow widths', () => {
    /* CSS contract: the responsive blocks at the end of the stylesheet. */
    const media900 = css.slice(css.indexOf('@media (max-width: 900px)'), css.indexOf('@media (max-width: 560px)'))
    const media560 = css.slice(css.indexOf('@media (max-width: 560px)'))
    expect(media900).toContain('.writes-layout { grid-template-columns: 1fr }')
    expect(media560).toContain('.write-card-foot { flex-direction: column')
    expect(media560).toContain('.write-actions .write-btn { flex: 1 }')
    expect(media560).toContain('.write-actions .write-action-error { width: 100% }')
  })
})
