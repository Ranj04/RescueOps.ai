import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Frontend source lives in web/ (ARCHITECTURE §8 Track B). index.html at the
// repo root is the Makers convention the builder auto-detects (framework: vite).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Local dev talks to the deployed API; prod is same-origin.
      "/api": {
        target: process.env.VITE_API_PROXY || "https://rescueops-dpj9utykdvs3.edgeone.dev",
        changeOrigin: true,
      },
    },
  },
});
