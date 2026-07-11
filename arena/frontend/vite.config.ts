import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发期 npm run dev: 把 /api 代理到 arena serve 的 web 端口(默认 50180)
    proxy: {
      '/api': 'http://127.0.0.1:50180',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
