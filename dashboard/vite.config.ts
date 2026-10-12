/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const BACKEND_URL = process.env.VIBONY_BACKEND_URL ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/sessions': {
        target: BACKEND_URL,
        changeOrigin: true,
        ws: true,
        bypass(req) {
          if (req.headers.accept?.includes('text/html')) {
            return '/index.html';
          }
        },
      },
      '/auth': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/workspaces': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/invites': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      '/tasks': {
        target: BACKEND_URL,
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/tests/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
});
