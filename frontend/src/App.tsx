import { useSessionSocket } from './lib/useSessionSocket'
import { StatusStrip } from './components/StatusStrip'
import { Leaderboard } from './components/Leaderboard'

/**
 * Tier A shell: status strip across the top, leaderboard filling the rest.
 *
 * The track map (top right) and radio panel (bottom right) are Tier B and are
 * deliberately absent rather than stubbed - an empty panel on a race Friday is
 * a distraction, and the build plan asks for a bare leaderboard and nothing
 * else before the session.
 */
export default function App() {
  useSessionSocket()

  return (
    <div className="flex h-full flex-col">
      <StatusStrip />

      <main className="min-h-0 flex-1 bg-f1-bg">
        <Leaderboard />
      </main>

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
