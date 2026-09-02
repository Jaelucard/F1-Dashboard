import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusStrip } from './StatusStrip'
import { useStore } from '../store'
import type { FeedInfo, SessionState } from '../types/sessionState'
import sampleSnapshot from '../test/sampleSnapshot.json'

const snapshot = sampleSnapshot as unknown as SessionState

function withFeed(feed: Partial<FeedInfo> | null, overrides: Partial<SessionState> = {}): SessionState {
  return {
    ...snapshot,
    ...overrides,
    feed: feed === null ? null : { ...(snapshot.feed as FeedInfo), ...feed },
  }
}

function inject(state: SessionState, status: 'connecting' | 'open' | 'closed' = 'open', ageMs = 0) {
  useStore.setState({ snapshot: state, status, lastMessageAt: Date.now() - ageMs })
}

beforeEach(() => {
  useStore.setState({ snapshot: null, status: 'connecting', lastMessageAt: null })
})

describe('StatusStrip feed states', () => {
  it('shows LIVE when the feed is live', () => {
    inject(withFeed({ state: 'live' }))
    render(<StatusStrip />)
    expect(screen.getByText('LIVE')).toBeInTheDocument()
  })

  it('shows STALE, not LIVE, when the upstream feed has gone quiet', () => {
    inject(withFeed({ state: 'stale', data_age_seconds: 42 }))
    render(<StatusStrip />)
    expect(screen.getByText('STALE')).toBeInTheDocument()
    expect(screen.queryByText('LIVE')).not.toBeInTheDocument()
  })

  it('shows WAITING when MQTT is connected but nothing has arrived', () => {
    inject(withFeed({ state: 'connected', data_age_seconds: null, last_message_at: null }))
    render(<StatusStrip />)
    expect(screen.getByText('WAITING')).toBeInTheDocument()
  })

  it('shows CONNECTING and AUTH FAILED for the recorder states', () => {
    inject(withFeed({ state: 'connecting', mqtt_connected: false }))
    const view = render(<StatusStrip />)
    expect(screen.getByText('CONNECTING')).toBeInTheDocument()
    view.unmount()

    inject(withFeed({ state: 'auth_failed', last_error: 'authentication failed (credentials)' }))
    render(<StatusStrip />)
    expect(screen.getByText('AUTH FAILED')).toBeInTheDocument()
  })

  it('shows DEMO for a synthetic grid, never LIVE', () => {
    inject(withFeed(null, { mode: 'demo' }))
    render(<StatusStrip />)
    expect(screen.getByText('DEMO')).toBeInTheDocument()
    expect(screen.queryByText('LIVE')).not.toBeInTheDocument()
  })

  it('shows OFFLINE when the socket is closed', () => {
    inject(withFeed({ state: 'live' }), 'closed')
    render(<StatusStrip />)
    expect(screen.getByText('OFFLINE')).toBeInTheDocument()
  })

  it('shows STALE when the backend stops pushing frames', () => {
    inject(withFeed({ state: 'live' }), 'open', 30_000)
    render(<StatusStrip />)
    expect(screen.getByText('STALE')).toBeInTheDocument()
    // Both ages read 30s: the socket age, and the feed age advanced by it.
    expect(screen.getAllByText('30s')).toHaveLength(2)
  })

  it('shows the upstream data age from the recorder, separately from the socket age', () => {
    inject(withFeed({ state: 'live', data_age_seconds: 3.2 }))
    render(<StatusStrip />)
    expect(screen.getByText('Feed age')).toBeInTheDocument()
    expect(screen.getByText('3s')).toBeInTheDocument()
  })

  it('flags a degraded frame', () => {
    inject(withFeed({ state: 'live' }, { degraded: true, degraded_reason: 'snapshot failed: RuntimeError' }))
    render(<StatusStrip />)
    expect(screen.getByText('DEGRADED')).toBeInTheDocument()
  })

  it('warns when the recording is failing even though data is live', () => {
    inject(withFeed({ state: 'live', recording_ok: false }))
    render(<StatusStrip />)
    expect(screen.getByText('RECORDING FAILED')).toBeInTheDocument()
    expect(screen.getByText('LIVE')).toBeInTheDocument()
  })
})
