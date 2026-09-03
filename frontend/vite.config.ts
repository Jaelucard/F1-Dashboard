import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The dev server proxies /health and /ws to the FastAPI backend on :8000.
// That keeps the browser on a single origin, so there is no CORS handshake and
// no backend URL baked into the frontend build.
export default defineConfig({
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
