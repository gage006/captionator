import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { version } from './package.json'

// The frontend ships as a static build served by nginx (see Dockerfile).
// There is no dev server, so no host/proxy/allowedHosts config is needed —
// in production the edge nginx proxies /api to the backend.
export default defineConfig({
  plugins: [react()],
  // package.json is the single source of truth for the app version; CI
  // rejects release tags that don't match it.
  define: {
    __APP_VERSION__: JSON.stringify(version),
  },
})
