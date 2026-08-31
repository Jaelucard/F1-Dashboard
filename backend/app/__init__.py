"""F1 timing dashboard backend.

PHASE is the single source of truth for which build phase is deployed. It is
reported by /health and on every WebSocket snapshot, so the browser and the
operator always agree about what the backend is capable of.
"""

PHASE = 1
