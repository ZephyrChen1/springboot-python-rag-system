import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 端口被占就直接报错，不要偷偷换端口 —— 否则你记的地址会失效
    strictPort: true,
    proxy: {
      '/api': {
        // 用 127.0.0.1 而不是 localhost：
        // Windows 上 localhost 可能先解析到 ::1，而 Tomcat 只监听 IPv4 时
        // 会得到一个非常难查的 ECONNREFUSED
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        // 不 rewrite：Java 侧暴露的就是 /api/rag/*，原样转发即可。
        // 浏览器只看到 5173 这一个源 —— 同源策略不触发，后端不需要任何 CORS 配置
        timeout: 600000,
        proxyTimeout: 600000,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
