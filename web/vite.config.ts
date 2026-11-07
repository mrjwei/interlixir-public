import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// In production CloudFront serves the SPA, /a3/* and /v1/auth/* from one origin. The dev server
// mirrors that with a proxy, so the browser never makes a cross-origin API call and the auth service's CORS stays as is.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const api = env.API_ORIGIN || 'https://auth.example.com'
  // /acp has no API Gateway route: the ALB only forwards it with CloudFront's secret header, so the dev server
  // goes through the distribution itself.
  const acp = env.ACP_ORIGIN || 'https://d1234abcd5678.cloudfront.net'
  return {
    plugins: [react(), tailwindcss()],
    resolve: { alias: { '@': path.resolve(import.meta.dirname, 'src') } },
    // ~175 KB gzipped for the app shell; the Markdown renderer is split out (job page only).
    build: { chunkSizeWarningLimit: 600 },
    server: {
      port: 5173,
      strictPort: true, // the uploads bucket's CORS rule allows exactly http://localhost:5173
      proxy: {
        '/a3': { target: api, changeOrigin: true },
        '/v1/auth': { target: api, changeOrigin: true },
        '/acp': { target: acp, changeOrigin: true },
      },
    },
  }
})
