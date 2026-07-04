import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const proxyHeaders = env.CALYX_WEB_API_BEARER_SECRET
    ? { authorization: `Bearer ${env.CALYX_WEB_API_BEARER_SECRET}` }
    : undefined;
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": {
          target: env.CALYX_WEB_API_PROXY_TARGET ?? "http://127.0.0.1:8121",
          changeOrigin: false,
          headers: proxyHeaders,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
    },
  };
});
