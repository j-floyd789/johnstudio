import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// JohnStudio desktop UI. Talks to the local FastAPI server on 127.0.0.1:8765.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  // Tauri target: when wrapped, the same build output is served by the Tauri shell.
  clearScreen: false,
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
