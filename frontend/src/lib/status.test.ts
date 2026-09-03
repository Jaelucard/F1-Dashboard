import { describe, expect, it } from 'vitest'
import { feedBadge, SOCKET_STALE_SECONDS } from './status'
import type { FeedInfo, SessionState } from '../types/sessionState'

function snapshot(overrides: Partial<SessionState> = {}, feed: Partial<FeedInfo> | null = null): SessionState {
  return {
    type: 'snapshot',
    phase: 2,
    mode: 'live',
    server_time: '2026-09-04T11:46:05.000+00:00',
    credentials_present: true,
    session: null,
    drivers: [],
    track_flag: null,
    session_status: null,
    partial_aero: false,
    session_best_lap: null,
    race_control: [],
    recorder: null,
    replay: null,
    feed:
      feed === null
        ? null
        : {
            state: 'live',
            mqtt_connected: true,
            authenticated: true,
            last_message_at: null,
            data_age_seconds: 0.5,
            stale_after_seconds: 15,
            recording_ok: true,
            last_error: null,
            ...feed,
          },
    adapter: null,
    degraded: false,
    degraded_reason: null,
    ...overrides,
  }
}

describe('feedBadge', () => {
  it('is LIVE only when the socket is open, the mode is live and the feed is live', () => {
    const badge = feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'live' }), dataAgeSeconds: 0 })
    expect(badge).toEqual({ label: 'LIVE', tone: 'live', pulse: true })
  })

  it('never says LIVE just because a snapshot in live mode arrived', () => {
    expect(feedBadge({ status: 'open', snapshot: snapshot({}, null), dataAgeSeconds: 0 }).label).not.toBe('LIVE')
    expect(feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'connected' }), dataAgeSeconds: 0 }).label).toBe('WAITING')
    expect(feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'connecting' }), dataAgeSeconds: 0 }).label).toBe('CONNECTING')
  })

  it('reports a stale upstream feed as STALE', () => {
    const badge = feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'stale' }), dataAgeSeconds: 0 })
    expect(badge.label).toBe('STALE')
    expect(badge.tone).toBe('bad')
  })

  it('reports an authentication failure and an offline recorder', () => {
    expect(feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'auth_failed' }), dataAgeSeconds: 0 }).label).toBe('AUTH FAILED')
    expect(feedBadge({ status: 'open', snapshot: snapshot({}, { state: 'offline' }), dataAgeSeconds: 0 }).label).toBe('OFFLINE')
  })

  it('keeps DEMO clearly separate from LIVE', () => {
    const badge = feedBadge({ status: 'open', snapshot: snapshot({ mode: 'demo' }, { state: 'live' }), dataAgeSeconds: 0 })
    expect(badge).toEqual({ label: 'DEMO', tone: 'demo', pulse: false })
  })

  it('shows IDLE for an idle backend', () => {
    expect(feedBadge({ status: 'open', snapshot: snapshot({ mode: 'idle' }, null), dataAgeSeconds: 0 }).label).toBe('IDLE')
  })

  it('shows OFFLINE when the socket is closed, whatever the last snapshot said', () => {
    const badge = feedBadge({ status: 'closed', snapshot: snapshot({}, { state: 'live' }), dataAgeSeconds: 1 })
    expect(badge).toEqual({ label: 'OFFLINE', tone: 'bad', pulse: false })
  })

  it('shows CONNECTING before the first frame', () => {
    expect(feedBadge({ status: 'connecting', snapshot: null, dataAgeSeconds: null }).label).toBe('CONNECTING')
    expect(feedBadge({ status: 'open', snapshot: null, dataAgeSeconds: null }).label).toBe('CONNECTING')
  })

  it('shows STALE when the socket is open but frames have stopped', () => {
    const badge = feedBadge({
      status: 'open',
      snapshot: snapshot({}, { state: 'live' }),
      dataAgeSeconds: SOCKET_STALE_SECONDS,
    })
    expect(badge.label).toBe('STALE')
    expect(badge.tone).toBe('bad')
  })

  it('marks a degraded frame without claiming LIVE', () => {
    const badge = feedBadge({
      status: 'open',
      snapshot: snapshot({ degraded: true, degraded_reason: 'snapshot failed: RuntimeError' }, { state: 'live' }),
      dataAgeSeconds: 0,
    })
    expect(badge.label).toBe('DEGRADED')
    expect(badge.tone).toBe('warn')
  })
})
