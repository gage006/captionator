import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The frontend ships as a static build served by nginx (see Dockerfile).
// There is no dev server, so no host/proxy/allowedHosts config is needed —
// in production the edge nginx proxies /api to the backend.
export default defineConfig({
  plugins: [react()],
})
