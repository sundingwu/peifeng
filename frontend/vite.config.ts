import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 构建产物给 FastAPI 托管（backend/main.py 只认 dist/）。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // antd 全量组件库本身就近 800 KB（gzip 253 KB），这是分包后的最大块，属预期
    chunkSizeWarningLimit: 900,
    // 把体积大且很少变动的依赖拆开：改业务代码时它们的哈希不变，浏览器可复用缓存
    // 注意：rolldown 的 manualChunks 只接受函数（rollup 才支持对象形式）
    rolldownOptions: {
      output: {
        manualChunks: (id: string) => {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("antd") || id.includes("@ant-design") || id.includes("rc-")) return "antd";
          if (id.includes("react") || id.includes("scheduler")) return "react";
          if (id.includes("@tanstack")) return "data";
          return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
    // 开发时把 /api 代理到本地后端，前后端各自热更新
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
});
