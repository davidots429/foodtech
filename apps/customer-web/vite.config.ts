import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => ({ plugins: [react()], server: {
  port: 5173, strictPort: true,
  proxy: { "/api": loadEnv(mode, ".", "FOODTECH_").FOODTECH_API_PROXY_TARGET || "http://127.0.0.1:8000" },
} }));

