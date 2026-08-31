import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { formatDataAge, formatSessionClock, NO_DATA } from '../lib/format'

/**
 * The top strip: what session, how far into it, what flag, and - the part that
 * matters most on a race Friday - how long since data last arrived.
 *
 * The data-age counter is the single clearest signal that the feed is alive.
 * The backend pushes a snapshot every second whether or not anything changed,
 * so this should sit at 0-1s. Anything above a few seconds means the socket,
 * the backend, or the MQTT connection is in trouble.
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

/** Ticks once a second so the clock and the age counter advance. */
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

  const dataAgeSeconds =
    lastMessageAt === null ? null : Math.max(0, Math.floor((now - lastMessageAt) / 1000))

  // Stale once we have missed several pushes in a row.
  const stale = dataAgeSeconds !== null && dataAgeSeconds >= 5
  const live = status === 'open' && snapshot?.mode === 'live'

  const flag = snapshot?.track_flag ?? null
  const flagStyle = flag ? (FLAG_STYLES[flag] ?? 'bg-f1-line text-f1-text') : null

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

      <div className="ml-auto flex items-center gap-4">
        {snapshot?.session_status && (
          <span className="hidden text-xs text-f1-muted lg:inline">{snapshot.session_status}</span>
        )}

        <Field
          label="Data age"
          value={formatDataAge(dataAgeSeconds)}
          tone={stale ? 'bad' : 'good'}
        />

        {recorder && (
          <Field
            label="Recorded"
            value={recorder.messages_recorded.toLocaleString()}
            title={
              recorder.token_expires_at
                ? `Token expires ${recorder.token_expires_at}`
                : undefined
            }
          />
        )}

        <span
          className={`flex items-center gap-1.5 rounded px-2 py-1 text-xs font-bold tracking-wide ${
            live ? 'bg-f1-red text-white' : 'bg-f1-line text-f1-muted'
          }`}
        >
          <span
            className={`h-2 w-2 rounded-full ${
              status === 'open' ? 'bg-white' : 'bg-f1-muted'
            } ${live ? 'animate-pulse' : ''}`}
            aria-hidden
          />
          {live ? 'LIVE' : status === 'open' ? (snapshot?.mode ?? 'IDLE').toUpperCase() : 'OFFLINE'}
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
