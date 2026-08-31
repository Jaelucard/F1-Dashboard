/**
 * Phase 0 placeholder for the shared SessionState type.
 *
 * From Phase 2 this file is GENERATED from the pydantic models in
 * backend/app/models.py, so the two sides cannot drift. Do not hand-edit it
 * once generation is wired up.
 */

export interface Snapshot {
  type: 'snapshot'
  server_time: string
  phase: number
  mode: 'live' | 'replay' | 'historical' | 'idle'
  credentials_present: boolean
  session: unknown | null
  drivers: unknown[]
}

export type ConnectionStatus = 'connecting' | 'open' | 'closed'
