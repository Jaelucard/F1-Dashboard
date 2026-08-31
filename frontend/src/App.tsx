import { useDataAge } from './lib/useDataAge'
import { useSessionSocket } from './lib/useSessionSocket'

/**
 * Phase 0 shell. Its only job is to prove the pipe: FastAPI -> WebSocket ->
 * React. Phase 2 replaces the body with StatusStrip + Leaderboard, keeping this
 * same socket hook.
 */
export default function App() {
  const { status, snapshot, lastMessageAt, reconnectAttempts } = useSessionSocket()
  const dataAge = useDataAge(lastMessageAt)

  const label =
    status === 'open' ? 'Connected' : status === 'connecting' ? 'Connecting…' : 'Disconnected'

  const dotColour =
    status === 'open'
      ? 'bg-timing-personal'
      : status === 'connecting'
        ? 'bg-timing-slower'
        : 'bg-f1-red'

  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-3 border-b border-f1-line bg-f1-panel px-6 py-3">
        <span className="h-6 w-1.5 rounded-sm bg-f1-red" aria-hidden />
        <h1 className="text-lg font-bold tracking-wide uppercase">F1 Timing</h1>
        <span className="ml-auto flex items-center gap-2 text-sm">
          <span className={`h-2.5 w-2.5 rounded-full ${dotColour}`} aria-hidden />
          <span className="font-semibold">{label}</span>
        </span>
      </header>

      <main className="flex flex-1 items-center justify-center p-8">
        <div className="w-full max-w-md rounded-lg border border-f1-line bg-f1-panel p-6">
          <p className="text-3xl font-bold">{label}</p>
          <dl className="mt-5 space-y-2 text-sm">
            <Row label="Data age" value={dataAge === null ? '—' : `${dataAge}s`} mono />
            <Row label="Mode" value={snapshot?.mode ?? '—'} />
            <Row label="Phase" value={snapshot ? String(snapshot.phase) : '—'} mono />
            <Row
              label="Credentials"
              value={
                snapshot === null ? '—' : snapshot.credentials_present ? 'loaded' : 'not set'
              }
            />
            {reconnectAttempts > 0 && (
              <Row label="Reconnect attempts" value={String(reconnectAttempts)} mono />
            )}
          </dl>
        </div>
      </main>

      <footer className="border-t border-f1-line px-6 py-3 text-xs text-f1-muted">
        Data from the{' '}
        <a className="underline hover:text-f1-text" href="https://openf1.org">
          OpenF1 API
        </a>
        . Unofficial project, not affiliated with Formula 1.
      </footer>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between border-b border-f1-line/60 pb-2 last:border-0">
      <dt className="text-f1-muted">{label}</dt>
      <dd className={mono ? 'tnum font-semibold' : 'font-semibold'}>{value}</dd>
    </div>
  )
}
