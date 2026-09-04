import { createLogger, defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * Quiets one specific, harmless log: when the `/ws` tunnel's underlying TCP
 * connection is severed mid-write - a browser tab closed or refreshed, a
 * laptop slept, a brief network drop - Vite's own ws-proxy dumps a raw Node
 * stack trace as "ws proxy error" / "ws proxy socket error". There is no app
 * code in that stack to act on: it is Vite's http-proxy noticing the pipe is
 * gone, and both ends of this app already handle the disconnect on their own
 * (`ws.py` logs "browser disconnected" and drops the client; `socketController.ts`
 * reconnects on a backoff timer). Over a multi-hour session with laptops
 * sleeping and tabs refreshing, this fires often enough to bury real errors
 * in the terminal, so it is filtered by error code - never by message text
 * alone, so an unrelated ws-proxy failure still surfaces normally.
 */
const BENIGN_DISCONNECT_CODES = new Set(['EPIPE', 'ECONNRESET', 'ECONNABORTED'])

const logger = createLogger()
const logError = logger.error.bind(logger)
logger.error = (msg, options) => {
  const code = (options?.error as NodeJS.ErrnoException | undefined)?.code
  const isWsProxyNoise = msg.includes('ws proxy error') || msg.includes('ws proxy socket error')
  if (isWsProxyNoise && code !== undefined && BENIGN_DISCONNECT_CODES.has(code)) return
  logError(msg, options)
}

// The dev server proxies /health and /ws to the FastAPI backend on :8000.
// That keeps the browser on a single origin, so there is no CORS handshake and
// no backend URL baked into the frontend build.
export default defineConfig({
  customLogger: logger,
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/replay': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
