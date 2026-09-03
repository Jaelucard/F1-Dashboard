import { useEffect, useState } from 'react'
import { selectDrivers, selectSession, useStore } from '../store'
import { teamColour } from '../lib/teams'
import { useTrackTrace, type Point } from '../lib/useTrackTrace'
import { loadOutline, type Outline } from '../data/circuits'
import type { DriverState } from '../types/sessionState'

/**
 * The track map: a bundled circuit outline (or, failing that, the trace of
 * where the cars have been) with one dot per car in team colour.
 *
 * Coordinates are OpenF1's own `location` frame, in which y grows northwards;
 * SVG's y grows downwards, so y is negated everywhere. The viewBox is fitted
 * to the outline so every circuit fills the panel regardless of its size,
 * and the dot radius and stroke are derived from that size so they look the
 * same on Monaco and on Spa.
 *
 * Snapshots arrive once a second, so each dot glides to its new position with
 * a one-second linear transform transition rather than jumping.
 */

interface Bounds {
  minX: number
  minY: number
  width: number
  height: number
}

function bounds(points: Point[], cars: DriverState[]): Bounds | null {
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  const consider = (x: number, y: number) => {
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (-y < minY) minY = -y
    if (-y > maxY) maxY = -y
  }
  for (const [x, y] of points) consider(x, y)
  for (const car of cars) consider(car.x as number, car.y as number)
  if (!Number.isFinite(minX)) return null
  const spanX = Math.max(maxX - minX, 1)
  const spanY = Math.max(maxY - minY, 1)
  const pad = Math.max(spanX, spanY) * 0.06
  return { minX: minX - pad, minY: minY - pad, width: spanX + 2 * pad, height: spanY + 2 * pad }
}

function pathFrom(points: Point[], closed: boolean): string {
  const parts = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x} ${-y}`)
  return parts.join(' ') + (closed ? ' Z' : '')
}

export function TrackMap() {
  const drivers = useStore(selectDrivers)
  const session = useStore(selectSession)
  const circuitKey = session?.circuit_key ?? null
  const [loaded, setLoaded] = useState<{ key: number | null; outline: Outline | null }>({ key: null, outline: null })

  useEffect(() => {
    let alive = true
    void loadOutline(circuitKey).then((outline) => {
      if (alive) setLoaded({ key: circuitKey, outline })
    })
    return () => {
      alive = false
    }
  }, [circuitKey])

  // Only trust an outline that was loaded for the circuit being shown.
  const outline = loaded.key === circuitKey ? loaded.outline : null
  const trace = useTrackTrace()
  const points = outline?.points ?? trace
  const cars = drivers.filter((d) => d.x != null && d.y != null)
  const box = bounds(points, cars)

  if (box === null) {
    return (
      <section aria-label="Track map" data-testid="track-map" className="flex h-full w-full items-center justify-center bg-f1-panel">
        <p className="text-xs text-f1-muted">Waiting for location data</p>
      </section>
    )
  }

  const scale = Math.max(box.width, box.height)
  const stroke = scale / 220
  const radius = scale / 55

  return (
    <section aria-label="Track map" data-testid="track-map" className="relative h-full w-full bg-f1-panel">
      <svg
        viewBox={`${box.minX} ${box.minY} ${box.width} ${box.height}`}
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
        role="img"
        aria-label={outline?.circuit_short_name ?? session?.circuit_short_name ?? 'track'}
      >
        {points.length > 1 && (
          <path
            data-testid="outline"
            d={pathFrom(points, outline !== null)}
            fill="none"
            stroke="var(--color-f1-line)"
            strokeWidth={stroke * 2.5}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        )}
        {cars.map((car) => (
          <g
            key={car.driver_number}
            data-driver={car.driver_number}
            data-in-pit={car.in_pit ? 'true' : 'false'}
            style={{
              transform: `translate(${car.x}px, ${-(car.y as number)}px)`,
              transition: 'transform 1s linear',
              opacity: car.in_pit ? 0.4 : 1,
            }}
          >
            <circle r={radius} fill={teamColour(car.team_colour, car.team_name)} stroke="var(--color-f1-bg)" strokeWidth={stroke} />
            <text
              y={-radius * 1.5}
              textAnchor="middle"
              fontSize={radius * 1.7}
              fontWeight={700}
              fill="var(--color-f1-text)"
              style={{ paintOrder: 'stroke', stroke: 'var(--color-f1-bg)', strokeWidth: stroke }}
            >
              {car.name_acronym ?? car.driver_number}
            </text>
          </g>
        ))}
      </svg>
      <span className="absolute right-2 bottom-1 text-[10px] tracking-wide text-f1-muted uppercase">
        {outline ? outline.circuit_short_name : 'tracing'}
      </span>
    </section>
  )
}
