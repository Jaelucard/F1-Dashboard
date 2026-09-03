/**
 * The optional shared secret, if the backend has WS_AUTH_TOKEN set.
 *
 * One lookup for both the WebSocket (`?token=`) and the replay control API
 * (`Authorization: Bearer`), so the two can never disagree about where the
 * token comes from: the build-time VITE_WS_TOKEN first, then localStorage.
 */

export const TOKEN_STORAGE_KEY = 'f1dash.wsToken'

export function accessToken(): string | null {
  const fromBuild = import.meta.env.VITE_WS_TOKEN as string | undefined
  if (fromBuild) return fromBuild
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}
