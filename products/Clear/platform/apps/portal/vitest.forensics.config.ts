import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  cacheDir: process.env.CLEAR_UI_CACHE || 'F:/OCR/output/forensics-stability-20261014/cache/portal-vite',
  test: { environment: 'jsdom', maxWorkers: 2 },
})
