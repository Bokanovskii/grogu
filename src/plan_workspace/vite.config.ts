import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Invariants honoured here:
//  * base "./" so the bundle is origin-relative and the Python server can mount
//    it under /static without absolute-path assumptions.
//  * content hashes disabled (entry -> app.js, assets -> [name][extname]) so the
//    committed dist/ is byte-stable and the drift check is meaningful.
//  * no sourcemaps in dist.
//  * a single JS chunk (no code splitting) so there is exactly one app.js to
//    serve and to review as a diff.
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "[name].js",
        assetFileNames: (info) => {
          const name = info.names && info.names[0] ? info.names[0] : "asset";
          if (name.endsWith(".css")) return "app.css";
          return "[name][extname]";
        },
        manualChunks: undefined,
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5178,
  },
});
