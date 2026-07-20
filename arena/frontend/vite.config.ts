import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发期 npm run dev: 把 /api 代理到新平台 serve-web(默认端口 50280)。
    // 旧 serve(50180)冻结只读,里程碑 8 后不再用。
    proxy: {
      '/api': 'http://127.0.0.1:50280',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
