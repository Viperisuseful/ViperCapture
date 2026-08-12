import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

const tauriDevHost = process.env.TAURI_DEV_HOST

export default defineConfig(() => {
  return {
    base: './',
    clearScreen: false,
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(import.meta.dirname, './src'),
      },
    },
    envPrefix: ['VITE_', 'TAURI_ENV_*'],
    server: {
      port: 1420,
      strictPort: true,
      host: tauriDevHost || false,
      hmr: tauriDevHost
        ? {
            protocol: 'ws',
            host: tauriDevHost,
            port: 1421,
          }
        : undefined,
      watch: {
        ignored: ['**/src-tauri/**'],
      },
    },
    build: {
      outDir: './dist',
      emptyOutDir: true,
      target: process.env.TAURI_ENV_PLATFORM === 'windows' ? 'chrome105' : 'safari13',
      minify: process.env.TAURI_ENV_DEBUG ? false : undefined,
      sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
    },
  }
})
