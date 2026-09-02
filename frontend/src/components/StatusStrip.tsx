import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { formatDataAge, formatSessionClock, NO_DATA } from '../lib/format'
import { feedBadge, SOCKET_STALE_SECONDS, type BadgeTone } from '../lib/status'

/**
 * The top strip: what session, how far into it, what flag, and - the part that
 * matters most on a race Friday - whether data is actually arriving.
 *
 * Two ages are shown because they answer different questions:
 *
 *   Data age  seconds since this browser received a frame. The backend pushes
 *             every second whether or not anything changed, so this sits at
 *             0-1s; more means the socket or the backend is in trouble.
 *   Feed age  seconds since the *backend* received an MQTT message, as
 *             reported by the recorder. This is what says whether OpenF1 is
 *             sending anything. It is what makes the LIVE badge honest.
 */

const FLAG_STYLES: Record<string, string> = {
  YELLOW: 'bg-timing-slower text-black',
  'DOUBLE YELLOW': 'bg-timing-slower text-black',
  RED: 'bg-f1-red text-white',
  GREEN: 'bg-timing-personal text-black',
  BLUE: 'bg-[#3B82F6] text-white',
  CHEQUERED: 'bg-white text-black',
  'BLACK AND WHITE': 'bg-white text-black',
}

const BADGE_STYLES: Record<BadgeTone, string> = {
  live: 'bg-f1-red text-white',
  demo: 'bg-timing-slower text-black',
  warn: 'bg-f1-line text-timing-slower',
  bad: 'bg-f1-line text-f1-red',
  idle: 'bg-f1-line text-f1-muted',
}

/** Ticks once a second so the clock and the age counters advance. */
function useSecondTick(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])
  return now
}

export function StatusStrip() {
  const now = useSecondTick()
  const snapshot = useStore((s) => s.snapshot)
  const status = useStore((s) => s.status)
  const lastMessageAt = useStore((s) => s.lastMessageAt)

  const session = snapshot?.session ?? null
  const recorder = snapshot?.recorder ?? null
  const feed = snapshot?.feed ?? null

  const dataAgeSeconds =
    lastMessageAt === null ? null : Math.max(0, Math.floor((now - lastMessageAt) / 1000))
  const socketStale = dataAgeSeconds !== null && dataAgeSeconds >= SOCKET_STALE_SECONDS

  // Upstream age: what the recorder reported, plus the time since that frame.
  const feedAgeSeconds =
    feed?.data_age_seconds === null || feed?.data_age_seconds === undefined
      ? null
      : Math.floor(feed.data_age_seconds + (dataAgeSeconds ?? 0))
  const feedStale =
    feedAgeSeconds !== null && feed !== null && feedAgeSeconds > feed.stale_after_seconds

  const badge = feedBadge({ status, snapshot, dataAgeSeconds })

  const flag = snapshot?.track_flag ?? null
  const flagStyle = flag ? (FLAG_STYLES[flag] ?? 'bg-f1-line text-f1-text') : null

  const problem =
    snapshot?.degraded && snapshot.degraded_reason
      ? snapshot.degraded_reason
      : feed?.last_error ?? null

  return (
    <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-f1-line bg-f1-panel px-4 py-2.5 md:px-6">
      <div className="flex items-center gap-3">
        <span className="h-6 w-1.5 rounded-sm bg-f1-red" aria-hidden />
        <div className="leading-tight">
          <div className="text-sm font-bold tracking-wide uppercase">
            {session?.session_name ?? 'No session'}
          </div>
          <div className="text-xs text-f1-muted">
            {session?.circuit_short_name ?? session?.location ?? 'waiting for session data'}
          </div>
        </div>
      </div>

      <Field label="Clock" value={formatSessionClock(session?.date_start ?? null, new Date(now))} />

      {flag && (
        <span className={`rounded px-2 py-0.5 text-xs font-bold tracking-wide ${flagStyle}`}>
          {flag}
        </span>
      )}

      {snapshot?.partial_aero && (
        <span
          className="rounded border border-timing-slower px-2 py-0.5 text-xs font-bold text-timing-slower"
          title="Race control has enabled partial aero: front in Straight Mode, rear in Corner Mode"
        >
          PARTIAL AERO
        </span>
      )}

      {feed && !feed.recording_ok && (
        <span
          className="rounded border border-f1-red px-2 py-0.5 text-xs font-bold text-f1-red"
          title={feed.last_error ?? 'Writes to the recordings directory are failing'}
        >
          RECORDING FAILED
        </span>
      )}

      <div className="ml-auto flex items-center gap-4">
        {snapshot?.session_status && (
          <span className="hidden text-xs text-f1-muted lg:inline">{snapshot.session_status}</span>
        )}

        <Field
          label="Data age"
          value={formatDataAge(dataAgeSeconds)}
          tone={socketStale ? 'bad' : 'good'}
          title="Seconds since this browser last received a frame from the backend"
        />

        {feed && (
          <Field
            label="Feed age"
            value={formatDataAge(feedAgeSeconds)}
            tone={feedStale || feed.state === 'stale' ? 'bad' : feed.state === 'live' ? 'good' : undefined}
            title="Seconds since the backend last received a message from OpenF1"
          />
        )}

        {recorder && (
          <Field
            label="Recorded"
            value={recorder.messages_recorded.toLocaleString()}
            tone={recorder.recording_ok ? undefined : 'bad'}
            title={[
              recorder.token_expires_at ? `Token expires ${recorder.token_expires_at}` : null,
              recorder.may_be_incomplete ? 'Recording may be incomplete' : null,
              recorder.disk_low ? 'Disk space is low' : null,
            ]
              .filter(Boolean)
              .join(' · ') || undefined}
          />
        )}

        <span
          className={`flex items-center gap-1.5 rounded px-2 py-1 text-xs font-bold tracking-wide ${BADGE_STYLES[badge.tone]}`}
          title={problem ?? undefined}
        >
          <span
            className={`h-2 w-2 rounded-full ${
              status === 'open' ? 'bg-white' : 'bg-f1-muted'
            } ${badge.pulse ? 'animate-pulse' : ''}`}
            aria-hidden
          />
          {badge.label}
        </span>
      </div>
    </header>
  )
}

function Field({
  label,
  value,
  tone,
  title,
}: {
  label: string
  value: string
  tone?: 'good' | 'bad'
  title?: string
}) {
  const toneClass =
    tone === 'bad' ? 'text-f1-red' : tone === 'good' ? 'text-timing-personal' : 'text-f1-text'
  return (
    <div className="leading-tight" title={title}>
      <div className="text-[10px] tracking-wider text-f1-muted uppercase">{label}</div>
      <div className={`tnum text-sm font-semibold ${toneClass}`}>{value || NO_DATA}</div>
    </div>
  )
}
