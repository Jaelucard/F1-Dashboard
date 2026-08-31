import { useEffect, useState } from 'react'

/**
 * Seconds since the last frame arrived, ticking once a second.
 *
 * The ticking clock is the state; the age is derived during render. Doing it
 * the other way round (recomputing the age inside the effect) means a new
 * message cannot be reflected until the next interval fires, and it sets state
 * from inside an effect for no reason.
 *
 * Kept out of useSessionSocket so the once-a-second re-render is confined to
 * whichever component shows the counter, rather than re-rendering the whole
 * leaderboard.
 */
export function useDataAge(lastMessageAt: number | null): number | null {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  if (lastMessageAt === null) return null
  // Clamp: a frame that arrived after the last tick would otherwise read as -1.
  return Math.max(0, Math.floor((now - lastMessageAt) / 1000))
}
