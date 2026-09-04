import { useStore } from '../store'
import { teamColour } from '../lib/teams'
import { TyreIcon } from './TyreIcon'
import { MiniSectors } from './MiniSectors'
import {
  formatGap,
  formatInterval,
  formatLapTime,
  formatTyreAge,
  lapTimeColour,
  NO_DATA,
  SECTOR_TOLERANCE_SECONDS,
  sectorColour,
  type TimingColour,
} from '../lib/format'
import type { DriverState } from '../types/sessionState'

/**
 * The Tier A leaderboard: POS, team colour bar, DRIVER, LAST, BEST, TYRE, GAP,
 * INT, S1, S2, S3, SPEED, THEO, PIT.
 *
 * Each sector cell is two lines: the sector time on top (coloured purple for
 * the session best, green for the driver's own best, yellow otherwise), the
 * mini-sector strip under it in its own separate colouring. The sector fields
 * come from the lap *in progress* once it has produced anything, so a cell
 * showing a strip with no time yet is the normal "sector filling in" state,
 * not missing data.
 *
 * There are deliberately no AERO or OT columns yet. 2026 replaced DRS with
 * Active Aero and Overtake Mode, but the only candidate field OpenF1 exposes is
 * the legacy `drs` integer whose 2026 meaning is unknown. Those columns arrive
 * in Tier B once the Monza FP1 recording says what that field contains - and
 * are dropped entirely if it turns out to be dead.
 *
 * Rows are keyed by driver number so React reuses each row across snapshots
 * rather than rebuilding the table when the order changes. Row order is
 * whatever `position` says - nothing here re-sorts client-side.
 *
 * Each cell carries a `data-col` attribute. Two columns in a row frequently
 * hold identical text - LAST equals BEST on a driver's fastest lap, GAP equals
 * INT in second place - so the text alone does not identify a cell, for a test
 * or for anything else that needs to address one column.
 */

/** Stable identity for the no-session-yet default, so selecting it does not
 * hand useSyncExternalStore a fresh array on every call - which is a
 * Maximum update depth loop with nothing actually changing. */
const EMPTY_SECTORS: (number | null)[] = [null, null, null]

const TIMING_CLASS: Record<TimingColour, string> = {
  best: 'text-timing-best',
  personal: 'text-timing-personal',
  slower: 'text-timing-slower',
  none: 'text-timing-none',
}

export function Leaderboard() {
  const drivers = useStore((s) => s.snapshot?.drivers)
  const sessionBest = useStore((s) => s.snapshot?.session_best_lap ?? null)
  const sessionBestSectors = useStore((s) => s.snapshot?.session_best_sectors ?? EMPTY_SECTORS)

  if (!drivers || drivers.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-f1-muted">
        <div>
          <p className="text-lg font-semibold">No timing data yet</p>
          <p className="mt-1 text-sm">
            The leaderboard fills in as the session starts and messages arrive.
          </p>
        </div>
      </div>
    )
  }

  // The minimum theoretical lap across every row, for the THEO column's best
  // colour. Computed here, once per snapshot, rather than per row.
  const theoreticals = drivers
    .map((d) => d.theoretical_lap)
    .filter((v): v is number => v !== null && Number.isFinite(v))
  const bestTheoretical = theoreticals.length > 0 ? Math.min(...theoreticals) : null

  return (
    <div className="h-full overflow-auto">
      <table className="w-full min-w-[880px] border-collapse text-sm whitespace-nowrap">
        <thead className="sticky top-0 z-10 bg-f1-panel">
          <tr className="text-[10px] tracking-wider text-f1-muted uppercase">
            <Th className="w-10 text-right">Pos</Th>
            <Th className="w-1 px-0" srOnly>
              Team colour
            </Th>
            <Th className="text-left">Driver</Th>
            <Th className="w-24 text-right">Last</Th>
            <Th className="w-24 text-right">Best</Th>
            <Th className="w-16 text-left">Tyre</Th>
            <Th className="w-24 text-right">Gap</Th>
            <Th className="w-24 text-right">Int</Th>
            <Th className="w-20 text-right">S1</Th>
            <Th className="w-20 text-right">S2</Th>
            <Th className="w-20 text-right">S3</Th>
            <Th className="w-16 text-right">Speed</Th>
            <Th className="w-20 text-right">Theo</Th>
            <Th className="w-12 text-center">Pit</Th>
          </tr>
        </thead>
        <tbody>
          {drivers.map((driver, index) => (
            <Row
              key={driver.driver_number}
              driver={driver}
              isLeader={index === 0 && driver.position === 1}
              sessionBest={sessionBest}
              sessionBestSectors={sessionBestSectors}
              bestTheoretical={bestTheoretical}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Row({
  driver,
  isLeader,
  sessionBest,
  sessionBestSectors,
  bestTheoretical,
}: {
  driver: DriverState
  isLeader: boolean
  sessionBest: number | null
  sessionBestSectors: (number | null)[]
  bestTheoretical: number | null
}) {
  const colour = teamColour(driver.team_colour, driver.team_name)

  const lastColour = lapTimeColour(driver.last_lap_duration, driver.best_lap_duration, sessionBest)
  const bestColour = lapTimeColour(driver.best_lap_duration, driver.best_lap_duration, sessionBest)

  const theoColour: TimingColour =
    driver.theoretical_lap !== null &&
    bestTheoretical !== null &&
    Math.abs(driver.theoretical_lap - bestTheoretical) <= SECTOR_TOLERANCE_SECONDS
      ? 'best'
      : 'none'

  return (
    <tr className="border-b border-f1-line/60 hover:bg-white/5">
      <td data-col="pos" className="tnum py-1.5 pr-2 text-right font-bold">
        {driver.position ?? NO_DATA}
      </td>

      <td data-col="colour" className="w-1 p-0">
        <div className="h-6 w-1" style={{ backgroundColor: colour }} aria-hidden />
      </td>

      <td data-col="driver" className="py-1.5 pl-2">
        <div className="flex items-baseline gap-2">
          <span className="font-bold tracking-wide">
            {driver.name_acronym ?? `#${driver.driver_number}`}
          </span>
          <span className="tnum text-xs text-f1-muted">{driver.driver_number}</span>
          <span className="hidden truncate text-xs text-f1-muted xl:inline">
            {driver.team_name ?? ''}
          </span>
        </div>
      </td>

      <td data-col="last" className={`tnum py-1.5 pr-2 text-right ${TIMING_CLASS[lastColour]}`}>
        {formatLapTime(driver.last_lap_duration)}
      </td>

      <td data-col="best" className={`tnum py-1.5 pr-2 text-right ${TIMING_CLASS[bestColour]}`}>
        {formatLapTime(driver.best_lap_duration)}
      </td>

      <td data-col="tyre" className="py-1.5">
        <div className="flex items-center gap-1.5">
          <TyreIcon compound={driver.compound} />
          <span className="tnum text-xs text-f1-muted">{formatTyreAge(driver.tyre_age)}</span>
        </div>
      </td>

      <td data-col="gap" className="tnum py-1.5 pr-2 text-right">
        {formatGap(driver.gap_to_leader, isLeader)}
      </td>

      <td data-col="int" className="tnum py-1.5 pr-2 text-right">
        {formatInterval(driver.interval_ahead)}
      </td>

      <SectorCell
        column="s1"
        time={driver.sector_1}
        segments={driver.segments_sector_1}
        personalBest={driver.best_sector_1}
        sessionBest={sessionBestSectors[0] ?? null}
      />
      <SectorCell
        column="s2"
        time={driver.sector_2}
        segments={driver.segments_sector_2}
        personalBest={driver.best_sector_2}
        sessionBest={sessionBestSectors[1] ?? null}
      />
      <SectorCell
        column="s3"
        time={driver.sector_3}
        segments={driver.segments_sector_3}
        personalBest={driver.best_sector_3}
        sessionBest={sessionBestSectors[2] ?? null}
      />

      <td data-col="speed" className="tnum py-1.5 pr-2 text-right text-xs text-f1-muted">
        {driver.speed === null || driver.speed === undefined ? NO_DATA : `${Math.round(driver.speed)} km/h`}
      </td>

      <td data-col="theo" className={`tnum py-1.5 pr-2 text-right text-xs ${TIMING_CLASS[theoColour]}`}>
        {formatLapTime(driver.theoretical_lap)}
      </td>

      <td data-col="pit" className="py-1.5 text-center">
        {driver.in_pit ? (
          <span className="rounded bg-f1-red px-1.5 py-0.5 text-[10px] font-bold text-white">
            PIT
          </span>
        ) : driver.is_pit_out_lap ? (
          <span className="rounded bg-f1-line px-1.5 py-0.5 text-[10px] font-bold text-f1-muted">
            OUT
          </span>
        ) : (
          <span className="text-f1-muted">·</span>
        )}
      </td>
    </tr>
  )
}

/**
 * One sector: the time (coloured against the session and personal best),
 * with the mini-sector strip beneath it.
 *
 * The strip is rendered even when the array is empty so the two-line height is
 * constant and rows do not jump as laps reset. The strip's own bar colours are
 * unrelated to the time's colour and are not touched here.
 */
function SectorCell({
  column,
  time,
  segments,
  personalBest,
  sessionBest,
}: {
  column: 's1' | 's2' | 's3'
  time: number | null
  segments: number[]
  personalBest: number | null
  sessionBest: number | null
}) {
  const colour = sectorColour(time, personalBest, sessionBest)
  return (
    <td data-col={column} className="py-1.5 pr-2 align-middle text-xs">
      <div className="flex flex-col items-stretch gap-1">
        <span className={`tnum text-right ${TIMING_CLASS[colour]}`}>{formatLapTime(time)}</span>
        <MiniSectors segments={segments} />
      </div>
    </td>
  )
}

function Th({
  children,
  className = '',
  srOnly = false,
}: {
  children: React.ReactNode
  className?: string
  srOnly?: boolean
}) {
  return (
    <th scope="col" className={`border-b border-f1-line px-2 py-2 font-semibold ${className}`}>
      {srOnly ? <span className="sr-only">{children}</span> : children}
    </th>
  )
}
