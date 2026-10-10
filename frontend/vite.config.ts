import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // So the desk can be opened from a phone through an ngrok tunnel.
    allowedHosts: ['.ngrok-free.app', '.ngrok.app'],
    // The backend through the same origin, so a phone that cannot see this
    // machine's 127.0.0.1 still reaches it - the tick streams included.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
