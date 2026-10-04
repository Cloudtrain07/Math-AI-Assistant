import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'node',
  },
  server: {
    proxy: {
      // 开发时前端 5173 → 后端 8000；浏览器视角始终同源，Cookie 与 Origin 校验对齐
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
})
