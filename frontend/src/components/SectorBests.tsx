import { useStore } from '../store'
import { teamColour } from '../lib/teams'
import { formatLapTime, NO_DATA } from '../lib/format'
import type { SectorLeader } from '../types/sessionState'

/**
 * Fastest three per sector: three columns (S1, S2, S3), each ranked from the
 * `best_sector_n` values already computed in the driver rows - no separate
 * lap scan, and it updates on its own as new laps arrive because it reads
 * straight from the store.
 */

const SECTORS: { label: string; index: 0 | 1 | 2 }[] = [
  { label: 'S1', index: 0 },
  { label: 'S2', index: 1 },
  { label: 'S3', index: 2 },
]

export function SectorBests() {
  const leaders = useStore((s) => s.snapshot?.sector_leaders ?? [[], [], []])

  return (
    <section
      aria-label="Fastest sectors"
      data-testid="sector-bests"
      className="grid shrink-0 grid-cols-3 gap-2 border-t border-f1-line bg-f1-panel p-3 text-xs"
    >
      {SECTORS.map(({ label, index }) => (
        <SectorColumn key={label} label={label} entries={leaders[index] ?? []} />
      ))}
    </section>
  )
}

function SectorColumn({ label, entries }: { label: string; entries: SectorLeader[] }) {
  return (
    <div data-col={label.toLowerCase()} className="flex flex-col gap-1">
      <div className="text-[10px] font-semibold tracking-wider text-f1-muted uppercase">{label}</div>
      {entries.length === 0 ? (
        <div className="text-f1-muted">{NO_DATA}</div>
      ) : (
        entries.map((entry, rank) => (
          <div key={entry.driver_number} className="flex items-center gap-1.5">
            <span
              className={`tnum w-3 text-right font-bold ${rank === 0 ? 'text-timing-best' : 'text-f1-muted'}`}
            >
              {rank + 1}
            </span>
            <span
              className="rounded px-1 py-0.5 text-[10px] font-bold text-f1-bg"
              style={{ backgroundColor: teamColour(entry.team_colour, null) }}
            >
              {entry.name_acronym ?? `#${entry.driver_number}`}
            </span>
            <span className={`tnum ml-auto ${rank === 0 ? 'text-timing-best' : 'text-f1-text'}`}>
              {formatLapTime(entry.time)}
            </span>
          </div>
        ))
      )}
    </div>
  )
}
