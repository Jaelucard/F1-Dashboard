import { useEffect } from 'react'
import { useStore } from '../store'
import { startSocketController } from './socketController'

/**
 * Wires the one WebSocket to the store.
 *
 * The lifecycle lives in `socketController.ts`; this hook only starts it on
 * mount and disposes it on unmount. Under React StrictMode the effect runs
 * twice (mount, unmount, mount): the first controller is disposed before the
 * second starts, so exactly one socket survives and no timer leaks.
 */

const TOKEN_STORAGE_KEY = 'f1dash.wsToken'

/** Optional shared secret, if the backend has WS_AUTH_TOKEN set. */
function accessToken(): string | null {
  const fromBuild = import.meta.env.VITE_WS_TOKEN as string | undefined
  if (fromBuild) return fromBuild
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

export function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = accessToken()
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return `${protocol}//${window.location.host}/ws${query}`
}

export function useSessionSocket(): void {
  useEffect(() => {
    const { applyFrame, setStatus, setReconnectAttempts } = useStore.getState()
    return startSocketController({
      url: socketUrl(),
      onFrame: (text) => {
        applyFrame(text)
      },
      onStatus: setStatus,
      onAttempts: setReconnectAttempts,
    })
  }, [])
}
