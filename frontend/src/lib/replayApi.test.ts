import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { listSessions, loadSession, play, seekFraction, setSpeed, unload } from './replayApi'
import { TOKEN_STORAGE_KEY } from './token'

type FetchMock = ReturnType<typeof vi.fn>

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

let fetchMock: FetchMock

/** Node 22+ ships a placeholder `localStorage` global that shadows jsdom's; use a plain in-memory one. */
function memoryStorage(): Storage {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear: () => store.clear(),
    getItem: (key) => store.get(key) ?? null,
    key: (index) => Array.from(store.keys())[index] ?? null,
    removeItem: (key) => void store.delete(key),
    setItem: (key, value) => void store.set(key, String(value)),
  }
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('localStorage', memoryStorage())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function lastRequest(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit]
  return { url, init }
}

describe('replayApi', () => {
  it('lists sessions from GET /replay/sessions', async () => {
    fetchMock.mockResolvedValue(respond(200, { sessions: [{ session_key: 42 }] }))
    const result = await listSessions()
    expect(result).toEqual({ ok: true, value: [{ session_key: 42 }] })
    expect(lastRequest().url).toBe('/replay/sessions')
    expect(lastRequest().init.method).toBe('GET')
  })

  it('posts JSON bodies to the control endpoints', async () => {
    fetchMock.mockResolvedValue(respond(200, { state: 'loaded' }))
    await loadSession(42)
    expect(lastRequest().url).toBe('/replay/load')
    expect(lastRequest().init.method).toBe('POST')
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ session_key: 42 })

    await seekFraction(1.7)
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ fraction: 1 })
    await setSpeed(5)
    expect(JSON.parse(lastRequest().init.body as string)).toEqual({ speed: 5 })
    await play()
    expect(lastRequest().url).toBe('/replay/play')
    await unload()
    expect(lastRequest().url).toBe('/replay/unload')
  })

  it('sends the shared token as a Bearer header when one is stored', async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, 'test-ws-token-placeholder')
    fetchMock.mockResolvedValue(respond(200, {}))
    await play()
    const headers = lastRequest().init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer test-ws-token-placeholder')
  })

  it('sends no Authorization header without a token', async () => {
    fetchMock.mockResolvedValue(respond(200, {}))
    await play()
    const headers = lastRequest().init.headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it("turns the backend's detail into an error result", async () => {
    fetchMock.mockResolvedValue(respond(409, { detail: 'replay is unavailable while demo mode is active' }))
    expect(await play()).toEqual({ ok: false, error: 'replay is unavailable while demo mode is active' })

    fetchMock.mockResolvedValue(respond(422, { detail: [{ msg: 'speed must be >= 0.25' }] }))
    expect(await setSpeed(0)).toEqual({ ok: false, error: 'speed must be >= 0.25' })

    fetchMock.mockResolvedValue(new Response('nope', { status: 502 }))
    expect(await play()).toEqual({ ok: false, error: 'HTTP 502' })
  })

  it('never throws on a network failure', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    expect(await listSessions()).toEqual({ ok: false, error: 'Failed to fetch' })
  })
})
