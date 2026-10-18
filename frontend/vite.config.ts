import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// The ADK FastAPI server runs on :8000 locally (see app/fast_api_app.py).
// We proxy its endpoints so the browser sees a single origin (no CORS) and
// SSE (/run_sse) streams through cleanly.
const ADK_TARGET = process.env.VITE_ADK_TARGET ?? "http://localhost:8000";

// Rewrite the Origin header to the ADK target. ADK's _OriginCheckMiddleware
// rejects state-changing (POST) requests whose Origin isn't same-origin or
// explicitly allow-listed; since the browser's Origin is :5173 but the request
// is proxied to :8000, we present it as same-origin so the agent accepts it
// (no ALLOW_ORIGINS env needed on the backend for local dev).
const rewriteOrigin = (proxy: { on: (e: string, cb: (req: unknown) => void) => void }) => {
  proxy.on("proxyReq", (proxyReq) => {
    (proxyReq as { setHeader: (k: string, v: string) => void }).setHeader("origin", ADK_TARGET);
  });
};

const proxy = Object.fromEntries(
  ["/apps", "/run", "/run_sse", "/list-apps", "/feedback", "/health", "/version", "/events", "/intake", "/config", "/invoices", "/stats"].map((p) => [
    p,
    { target: ADK_TARGET, changeOrigin: true, configure: rewriteOrigin },
  ]),
);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    proxy,
  },
  // `vite preview` (production build) uses its own proxy config.
  preview: {
    port: 4173,
    proxy,
  },
});
