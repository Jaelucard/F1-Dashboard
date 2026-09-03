import { useCallback, useEffect, useState } from 'react'
import { useStore } from '../store'
import {
  listSessions,
  loadSession,
  pause,
  play,
  seekFraction,
  setSpeed,
  unload,
  type ApiResult,
  type RecordingSummary,
} from '../lib/replayApi'
import type { ReplayInfo } from '../types/sessionState'

/**
 * Replay controls, shown only when the backend is idle (pick a recording) or
 * replaying (transport). In live and demo modes it renders nothing: the
 * backend refuses replay in those modes, so offering it would only produce
 * a 409.
 *
 * State comes from the snapshot the WebSocket already delivers, so the bar
 * never polls; a control call only triggers the change, and the next
 * snapshot shows it.
 */

const SPEEDS = [1, 2, 5, 10, 25]

const buttonClass =
  'rounded border border-f1-line bg-f1-bg px-3 py-1 text-xs font-bold tracking-wide uppercase hover:border-f1-muted disabled:opacity-40'

function clock(iso: string | null): string {
  if (!iso) return '--:--:--'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '--:--:--' : date.toISOString().slice(11, 19)
}

function sessionLabel(summary: RecordingSummary): string {
  const name = [summary.session_name, summary.circuit_short_name].filter(Boolean).join(' - ')
  const when = summary.start ? summary.start.slice(0, 10) : ''
  return `${summary.session_key}${name ? ` ${name}` : ''}${when ? ` (${when})` : ''}`
}

function useBusy() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = useCallback(async <T,>(action: () => Promise<ApiResult<T>>): Promise<T | null> => {
    setBusy(true)
    try {
      const result = await action()
      setError(result.ok ? null : result.error)
      return result.ok ? result.value : null
    } finally {
      setBusy(false)
    }
  }, [])
  return { busy, error, run, setError }
}

function Picker() {
  const [sessions, setSessions] = useState<RecordingSummary[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const { busy, error, run, setError } = useBusy()

  const accept = useCallback((list: RecordingSummary[]) => {
    setSessions(list)
    setSelected((current) => current ?? list[0]?.session_key ?? null)
  }, [])

  const refresh = useCallback(async () => {
    const list = await run(listSessions)
    if (list) accept(list)
  }, [run, accept])

  // The first listing is fetched directly: nothing is set until the reply arrives.
  useEffect(() => {
    let alive = true
    void listSessions().then((result) => {
      if (!alive) return
      if (result.ok) accept(result.value)
      else setError(result.error)
    })
    return () => {
      alive = false
    }
  }, [accept, setError])

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="text-xs font-bold tracking-wide text-f1-muted uppercase">Replay</span>
      {sessions !== null && sessions.length === 0 ? (
        <span className="text-xs text-f1-muted">No recordings found</span>
      ) : (
        <select
          aria-label="Recording"
          className="rounded border border-f1-line bg-f1-bg px-2 py-1 text-xs"
          value={selected ?? ''}
          onChange={(event) => setSelected(Number(event.target.value))}
          disabled={busy || sessions === null}
        >
          {(sessions ?? []).map((summary) => (
            <option key={summary.session_key} value={summary.session_key}>
              {sessionLabel(summary)}
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        className={buttonClass}
        disabled={busy || selected === null}
        onClick={() => selected !== null && void run(() => loadSession(selected))}
      >
        Load
      </button>
      <button type="button" className={buttonClass} disabled={busy} onClick={() => void refresh()}>
        Refresh
      </button>
      {error && <span className="text-xs text-f1-red">{error}</span>}
    </div>
  )
}

function Transport({ replay }: { replay: ReplayInfo }) {
  const { busy, error, run } = useBusy()
  const playing = replay.state === 'playing'

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="text-xs font-bold tracking-wide text-f1-muted uppercase">
        Replay {replay.session_key}
      </span>
      <button
        type="button"
        className={buttonClass}
        disabled={busy || replay.state === 'seeking'}
        onClick={() => void run(playing ? pause : play)}
      >
        {playing ? 'Pause' : 'Play'}
      </button>
      <select
        aria-label="Speed"
        className="rounded border border-f1-line bg-f1-bg px-2 py-1 text-xs"
        value={replay.speed}
        disabled={busy}
        onChange={(event) => void run(() => setSpeed(Number(event.target.value)))}
      >
        {SPEEDS.includes(replay.speed) ? null : <option value={replay.speed}>{replay.speed}x</option>}
        {SPEEDS.map((speed) => (
          <option key={speed} value={speed}>
            {speed}x
          </option>
        ))}
      </select>
      <span className="tnum text-xs text-f1-muted">{clock(replay.position)}</span>
      <input
        type="range"
        aria-label="Position"
        min={0}
        max={1000}
        value={Math.round(replay.progress * 1000)}
        disabled={busy}
        className="w-40 accent-f1-red md:w-72"
        onChange={(event) => void run(() => seekFraction(Number(event.target.value) / 1000))}
      />
      <span className="tnum text-xs text-f1-muted">{clock(replay.end)}</span>
      <span className="text-xs text-f1-muted uppercase">{replay.state}</span>
      <button type="button" className={buttonClass} disabled={busy} onClick={() => void run(unload)}>
        Unload
      </button>
      {error && <span className="text-xs text-f1-red">{error}</span>}
    </div>
  )
}

export function ReplayBar() {
  const snapshot = useStore((s) => s.snapshot)
  const mode = snapshot?.mode ?? null
  if (mode !== 'idle' && mode !== 'replay') return null
  const replay = snapshot?.replay ?? null

  return (
    <div className="border-t border-f1-line bg-f1-panel px-4 py-2 md:px-6" data-testid="replay-bar">
      {mode === 'replay' && replay ? <Transport replay={replay} /> : <Picker />}
    </div>
  )
}
