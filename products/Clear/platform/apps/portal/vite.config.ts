import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'CLEAR_UI_')
  return {
    plugins: [react()],
    cacheDir: env.CLEAR_UI_CACHE || 'node_modules/.vite',
    server: {
      host: '127.0.0.1',
      proxy: { '/api': env.CLEAR_UI_API || 'http://127.0.0.1:8766' },
    },
    build: { outDir: env.CLEAR_UI_DIST || 'dist' },
  }
})
