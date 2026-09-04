import { useStore } from '../store'
import { FLAG_STYLES } from '../lib/status'
import { formatSessionClock, formatUtcClock, NO_DATA } from '../lib/format'
import type { RaceControlMessage } from '../types/sessionState'

/**
 * Flag warnings panel: the current track flag and session status, active
 * marshal-sector yellows, a safety car / VSC banner, the last few driver
 * flags, and the last five race control messages so the reason behind a flag
 * is visible without leaving the page.
 *
 * Fixed height on desktop so the track map underneath keeps its space; on
 * small screens the panel is natural height and simply stacks above the map.
 *
 * No DRS anywhere - 2026 has none. `partial_aero` stays in StatusStrip.
 */

const DRIVER_FLAGS = new Set(['BLUE', 'BLACK AND WHITE', 'BLACK', 'BLACK AND ORANGE'])
const MESSAGE_LIMIT = 5
const DRIVER_FLAG_LIMIT = 3

export function FlagPanel() {
  const snapshot = useStore((s) => s.snapshot)
  const flag = snapshot?.track_flag ?? null
  const status = snapshot?.session_status ?? null
  const sectorFlags = snapshot?.sector_flags ?? []
  const safetyCar = snapshot?.safety_car ?? null
  const raceControl = snapshot?.race_control ?? []
  const drivers = snapshot?.drivers ?? []

  const acronymFor = (driverNumber: number | null): string => {
    if (driverNumber === null) return NO_DATA
    const driver = drivers.find((d) => d.driver_number === driverNumber)
    return driver?.name_acronym ?? `#${driverNumber}`
  }

  const driverFlags = raceControl
    .filter((m) => m.driver_number !== null && m.flag !== null && DRIVER_FLAGS.has(m.flag))
    .slice(-DRIVER_FLAG_LIMIT)
    .reverse()

  const recentMessages = raceControl.slice(-MESSAGE_LIMIT).reverse()

  const flagStyle = flag ? (FLAG_STYLES[flag] ?? 'bg-f1-line text-f1-text') : null

  return (
    <section
      aria-label="Flags"
      data-testid="flag-panel"
      className="flex shrink-0 flex-col gap-2 overflow-y-auto border-b border-f1-line bg-f1-panel p-3 lg:h-72 lg:border-b-0"
    >
      <div className="flex items-center gap-2">
        <span
          data-testid="track-flag"
          className={`rounded px-3 py-2 text-lg font-bold tracking-wide ${flagStyle ?? 'bg-f1-line text-f1-muted'}`}
        >
          {flag ?? 'NO FLAG'}
        </span>
        {status && <span className="text-xs text-f1-muted">{status}</span>}
      </div>

      {safetyCar && (
        <div
          data-testid="safety-car-banner"
          className="rounded border border-timing-slower bg-timing-slower/10 px-2 py-1.5 text-center text-sm font-bold tracking-wide text-timing-slower"
        >
          {safetyCar}
        </div>
      )}

      {sectorFlags.length > 0 && (
        <ul data-testid="sector-flags" className="flex flex-col gap-1">
          {sectorFlags.map((entry) => (
            <li
              key={entry.sector}
              data-sector={entry.sector}
              className="flex items-center justify-between rounded bg-f1-line/60 px-2 py-1 text-xs"
            >
              <span className="font-bold">
                S{entry.sector} {entry.flag}
              </span>
              <span className="tnum text-f1-muted">{formatSessionClock(entry.since, new Date())}</span>
            </li>
          ))}
        </ul>
      )}

      {driverFlags.length > 0 && (
        <div data-testid="driver-flags" className="flex flex-wrap gap-1.5">
          {driverFlags.map((entry, index) => (
            <DriverFlagChip key={index} entry={entry} acronym={acronymFor(entry.driver_number)} />
          ))}
        </div>
      )}

      <ul data-testid="race-control-messages" className="mt-auto flex flex-col gap-1 text-xs">
        {recentMessages.length === 0 && <li className="text-f1-muted">No messages yet</li>}
        {recentMessages.map((entry, index) => (
          <li key={index} className="flex gap-2">
            <span className="tnum shrink-0 text-f1-muted">{formatUtcClock(entry.date)}</span>
            <span className="truncate">{entry.message ?? NO_DATA}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function DriverFlagChip({ entry, acronym }: { entry: RaceControlMessage; acronym: string }) {
  const style = entry.flag ? (FLAG_STYLES[entry.flag] ?? 'bg-f1-line text-f1-text') : 'bg-f1-line text-f1-text'
  return (
    <span
      data-driver-flag={entry.driver_number}
      className={`rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide ${style}`}
    >
      {entry.flag} {acronym}
    </span>
  )
}
