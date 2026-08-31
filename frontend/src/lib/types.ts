/**
 * Frontend-only types.
 *
 * The wire types (SessionState, DriverState, ...) are NOT here: they are
 * generated from the pydantic models into `src/types/sessionState.ts`. Anything
 * describing a snapshot belongs there, so the two sides cannot drift.
 */

/** State of the WebSocket itself, which the server knows nothing about. */
export type ConnectionStatus = 'connecting' | 'open' | 'closed'
