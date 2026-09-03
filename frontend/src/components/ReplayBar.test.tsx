import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReplayBar } from './ReplayBar'
import { useStore } from '../store'
import type { ReplayInfo, SessionState } from '../types/sessionState'
import sampleSnapshot from '../test/sampleSnapshot.json'

vi.mock('../lib/replayApi', () => ({
  listSessions: vi.fn(),
  loadSession: vi.fn(),
  play: vi.fn(),
  pause: vi.fn(),
  seekFraction: vi.fn(),
  setSpeed: vi.fn(),
  unload: vi.fn(),
}))

import * as api from '../lib/replayApi'

const mocked = vi.mocked(api)
const snapshot = sampleSnapshot as unknown as SessionState

const replay: ReplayInfo = {
  session_key: 9001,
  state: 'paused',
  speed: 1,
  position: '2026-09-04T11:35:00.000+00:00',
  start: '2026-09-04T11:30:00.000+00:00',
  end: '2026-09-04T11:40:00.000+00:00',
  progress: 0.5,
  messages_replayed: 10,
  skipped_lines: 0,
  ingest_errors: 0,
}

function inject(overrides: Partial<SessionState>) {
  useStore.setState({ snapshot: { ...snapshot, ...overrides }, status: 'open', lastMessageAt: Date.now() })
}

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null })
  for (const fn of Object.values(mocked)) fn.mockReset()
  mocked.listSessions.mockResolvedValue({ ok: true, value: [] })
  for (const fn of [mocked.loadSession, mocked.play, mocked.pause, mocked.seekFraction, mocked.setSpeed]) {
    fn.mockResolvedValue({ ok: true, value: replay })
  }
  mocked.unload.mockResolvedValue({ ok: true, value: { state: 'idle' } })
})

describe('ReplayBar', () => {
  it('renders nothing in live and demo modes', () => {
    inject({ mode: 'live' })
    const { unmount } = render(<ReplayBar />)
    expect(screen.queryByTestId('replay-bar')).toBeNull()
    unmount()
    inject({ mode: 'demo' })
    render(<ReplayBar />)
    expect(screen.queryByTestId('replay-bar')).toBeNull()
  })

  it('offers the recordings on disk when idle and loads the chosen one', async () => {
    mocked.listSessions.mockResolvedValue({
      ok: true,
      value: [
        { session_key: 9002, topics: {}, size_bytes: 1, start: '2026-09-04T11:30:00+00:00', end: null, session_name: 'Practice 1', circuit_short_name: 'Monza' },
        { session_key: 9001, topics: {}, size_bytes: 1, start: null, end: null, session_name: null, circuit_short_name: null },
      ],
    })
    inject({ mode: 'idle', replay: null })
    render(<ReplayBar />)
    await waitFor(() => expect(screen.getByRole('option', { name: /9002 Practice 1 - Monza \(2026-09-04\)/ })).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Recording'), { target: { value: '9001' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load' }))
    await waitFor(() => expect(mocked.loadSession).toHaveBeenCalledWith(9001))
  })

  it('says so when there are no recordings', async () => {
    inject({ mode: 'idle', replay: null })
    render(<ReplayBar />)
    await waitFor(() => expect(screen.getByText('No recordings found')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Load' })).toBeDisabled()
  })

  it('shows the transport in replay mode and drives the API', async () => {
    inject({ mode: 'replay', replay })
    render(<ReplayBar />)
    expect(screen.getByText('Replay 9001')).toBeInTheDocument()
    expect(screen.getByText('11:35:00')).toBeInTheDocument()
    expect(screen.getByText('11:40:00')).toBeInTheDocument()
    expect(screen.getByLabelText('Position')).toHaveValue('500')

    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    await waitFor(() => expect(mocked.play).toHaveBeenCalled())

    fireEvent.change(screen.getByLabelText('Speed'), { target: { value: '10' } })
    await waitFor(() => expect(mocked.setSpeed).toHaveBeenCalledWith(10))

    fireEvent.change(screen.getByLabelText('Position'), { target: { value: '250' } })
    await waitFor(() => expect(mocked.seekFraction).toHaveBeenCalledWith(0.25))

    fireEvent.click(screen.getByRole('button', { name: 'Unload' }))
    await waitFor(() => expect(mocked.unload).toHaveBeenCalled())
  })

  it('shows Pause while playing', () => {
    inject({ mode: 'replay', replay: { ...replay, state: 'playing' } })
    render(<ReplayBar />)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it("surfaces the backend's reason when a control call fails", async () => {
    mocked.play.mockResolvedValue({ ok: false, error: 'replay is unavailable while live recording is active' })
    inject({ mode: 'replay', replay })
    render(<ReplayBar />)
    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    await waitFor(() => expect(screen.getByText('replay is unavailable while live recording is active')).toBeInTheDocument())
  })
})
