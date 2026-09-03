import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from './App'
import { useStore } from './store'
import type { SessionState } from './types/sessionState'
import sampleSnapshot from './test/sampleSnapshot.json'

vi.mock('./lib/useSessionSocket', () => ({ useSessionSocket: () => undefined }))
vi.mock('./lib/replayApi', () => ({
  listSessions: vi.fn().mockResolvedValue({ ok: true, value: [] }),
  loadSession: vi.fn(),
  play: vi.fn(),
  pause: vi.fn(),
  seekFraction: vi.fn(),
  setSpeed: vi.fn(),
  unload: vi.fn(),
}))

const snapshot = sampleSnapshot as unknown as SessionState

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null })
})

describe('App layout', () => {
  it('renders the strip, the leaderboard, the track map and the footer', () => {
    useStore.setState({ snapshot, status: 'open', lastMessageAt: Date.now() })
    render(<App />)
    expect(screen.getByRole('banner')).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByTestId('track-map')).toBeInTheDocument()
    expect(screen.getByText(/Unofficial project/)).toBeInTheDocument()
    expect(screen.queryByTestId('replay-bar')).toBeNull()
  })

  it('offers replay when the backend is idle', async () => {
    useStore.setState({ snapshot: { ...snapshot, mode: 'idle', drivers: [] }, status: 'open', lastMessageAt: Date.now() })
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('replay-bar')).toBeInTheDocument())
    expect(screen.getByText('No recordings found')).toBeInTheDocument()
  })
})
