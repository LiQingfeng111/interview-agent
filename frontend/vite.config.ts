import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 代理规则：开发时前端 5173，后端 8080。
// 用代理而不是直连的原因：避免 CORS 预检，且前端代码里可以写相对路径 /api/xxx，
// 打包后由 Nginx 同源转发，代码不用改。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
})
