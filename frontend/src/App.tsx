import { useSessionSocket } from './lib/useSessionSocket'
import { StatusStrip } from './components/StatusStrip'
import { Leaderboard } from './components/Leaderboard'
import { TrackMap } from './components/TrackMap'
import { ReplayBar } from './components/ReplayBar'

/**
 * Shell: status strip across the top, leaderboard filling the left, track
 * map in a fixed right-hand column on large screens (stacked above the
 * leaderboard below that), and the replay bar along the bottom whenever the
 * backend is idle or replaying.
 *
 * The radio panel (bottom right) is still Tier B and deliberately absent
 * rather than stubbed.
 */
export default function App() {
  useSessionSocket()

  return (
    <div className="flex h-full flex-col">
      <StatusStrip />

      <main className="flex min-h-0 flex-1 flex-col bg-f1-bg lg:flex-row">
        <div className="h-56 shrink-0 border-b border-f1-line lg:order-2 lg:h-auto lg:w-[26rem] lg:border-b-0 lg:border-l">
          <TrackMap />
        </div>
        <div className="min-h-0 flex-1 lg:order-1">
          <Leaderboard />
        </div>
      </main>

      <ReplayBar />

      <footer className="flex flex-wrap gap-x-2 border-t border-f1-line px-4 py-2 text-xs text-f1-muted md:px-6">
        <span>
          Data from the{' '}
          <a className="underline hover:text-f1-text" href="https://openf1.org">
            OpenF1 API
          </a>
          .
        </span>
        <span>Unofficial project, not affiliated with Formula 1.</span>
      </footer>
    </div>
  )
}
