import type { ReplayInfo } from '../types/sessionState'
import { accessToken } from './token'

/**
 * Typed wrappers over the backend's /replay control API.
 *
 * Nothing here throws: every call resolves to an `ApiResult`, so a component
 * can show the backend's reason ("replay is unavailable while live recording
 * is active") instead of an unhandled rejection. The token, when configured,
 * is sent as a Bearer header from the same lookup the WebSocket uses.
 */

export interface RecordingSummary {
  session_key: number
  topics: Record<string, number>
  size_bytes: number
  start: string | null
  end: string | null
  session_name: string | null
  circuit_short_name: string | null
}

export type ReplayStatus = ReplayInfo | { state: 'idle' }

export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: string }

function detailOf(data: unknown, status: number): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : ''))
        .filter(Boolean)
      if (messages.length) return messages.join('; ')
    }
  }
  return `HTTP ${status}`
}

async function call<T>(path: string, method: 'GET' | 'POST', body?: unknown): Promise<ApiResult<T>> {
  const headers: Record<string, string> = {}
  const token = accessToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  try {
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const text = await response.text()
    let data: unknown = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = null
    }
    if (!response.ok) return { ok: false, error: detailOf(data, response.status) }
    return { ok: true, value: data as T }
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : 'network error' }
  }
}

export async function listSessions(): Promise<ApiResult<RecordingSummary[]>> {
  const result = await call<{ sessions: RecordingSummary[] }>('/replay/sessions', 'GET')
  return result.ok ? { ok: true, value: result.value.sessions } : result
}

export function getReplay(): Promise<ApiResult<ReplayStatus>> {
  return call<ReplayStatus>('/replay', 'GET')
}

export function loadSession(sessionKey: number): Promise<ApiResult<ReplayInfo>> {
  return call<ReplayInfo>('/replay/load', 'POST', { session_key: sessionKey })
}

export function play(): Promise<ApiResult<ReplayInfo>> {
  return call<ReplayInfo>('/replay/play', 'POST')
}

export function pause(): Promise<ApiResult<ReplayInfo>> {
  return call<ReplayInfo>('/replay/pause', 'POST')
}

export function seekFraction(fraction: number): Promise<ApiResult<ReplayInfo>> {
  return call<ReplayInfo>('/replay/seek', 'POST', { fraction: Math.min(Math.max(fraction, 0), 1) })
}

export function setSpeed(speed: number): Promise<ApiResult<ReplayInfo>> {
  return call<ReplayInfo>('/replay/speed', 'POST', { speed })
}

export function unload(): Promise<ApiResult<{ state: 'idle' }>> {
  return call<{ state: 'idle' }>('/replay/unload', 'POST')
}
