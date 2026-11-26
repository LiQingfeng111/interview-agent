import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

/**
 * Vite 开发服务器配置。
 *
 * ## 代理规则
 *
 * 开发时前端 5173，后端 8080，通过 `/api` 代理转发。
 *
 * 为什么用代理而不是直连后端：
 * 1. 避免 CORS 预检（同源请求）
 * 2. 前端代码里写相对路径 `/api/xxx`，打包后由 Nginx 同源转发，**代码一行不用改**
 *
 * ## 为什么 host 必须是 '0.0.0.0'（默认是 localhost，会踩坑）
 *
 * Vite 默认 `host: 'localhost'`，在 Windows 上会绑到 **`::1`**（仅 IPv6 回环）。
 * 后果：**WSL / Docker 里完全访问不到**。
 *
 * 实测（本项目真实踩到）：
 *   Windows 侧  curl http://localhost:5173  → 200
 *   WSL 侧      curl 127.0.0.1:5173         → 000
 *   WSL 侧      curl <网关IP>:5173          → 000
 *   监听检查    netstat → `::1:5173`（只有 IPv6 回环）
 *
 * 结果就是 start.sh 明明服务在跑却报"前端还没起来"，
 * 而用 Docker 起后端的用户则完全连不上前端。
 *
 * 改成 '0.0.0.0' 后监听所有 IPv4 接口，WSL 和局域网都能访问。
 *
 * **安全提示**：`0.0.0.0` 意味着同局域网的其他设备也能访问你的开发服务器。
 * 如果只在单人机器上开发、不想暴露，用环境变量关掉：
 *
 *     VITE_HOST=localhost npm run dev
 */
const host = process.env.VITE_HOST || '0.0.0.0'

// 后端地址。默认 127.0.0.1:8080，可用环境变量覆盖
// （例如 Docker 里后端叫 backend:8080）。
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host,
    port: 5173,
    // 端口被占用时直接失败，不要静默换到 5174
    //
    // 理由：静默换端口会让用户按 README 打开 5173 却看到"无法访问"，
    // 而真正的前端在 5174。宁可报错让人看到"端口被占用"。
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  preview: {
    host,
    port: 5173,
    strictPort: true,
  },
})
