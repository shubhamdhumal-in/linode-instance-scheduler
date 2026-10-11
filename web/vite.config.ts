import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
export default defineConfig({
  base: '/ui/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
  },
  server: {
    proxy: {
      '/instances': 'http://127.0.0.1:8000',
      '/groups': 'http://127.0.0.1:8000',
      '/tokens': 'http://127.0.0.1:8000',
      '/linode': 'http://127.0.0.1:8000',
      '/login': 'http://127.0.0.1:8000',
      '/logout': 'http://127.0.0.1:8000',
      '/oauth': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/operations': 'http://127.0.0.1:8000',
      '/activity': 'http://127.0.0.1:8000',
      '/logs': 'http://127.0.0.1:8000',
      '/console': 'http://127.0.0.1:8000',
      '/backups': 'http://127.0.0.1:8000',
      '/pending-deletions': 'http://127.0.0.1:8000',
      '/holidays': 'http://127.0.0.1:8000',
      '/system': 'http://127.0.0.1:8000',
      '/schedule': 'http://127.0.0.1:8000',
    },
  },
})
