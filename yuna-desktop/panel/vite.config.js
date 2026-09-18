import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Build target is panel/ itself: local_bridge serves panel/index.html at `/`.
// Vite root is `src/` so `prebuild` (rm -rf ./assets ./index.html) can never
// wipe sources. outDir '..' == panel/. emptyOutDir must stay false because
// outDir is outside root (Vite requirement).
// NOTE: /assets/* URLs are served by the bridge from the PROJECT-ROOT assets/
// dir (local_bridge.py:477-479), NOT from panel/assets. `postbuild` copies the
// bundle there so the page actually loads. Deterministic file names
// (assets/app.js, assets/style.css) keep that copy idempotent.
export default defineConfig({
  root: 'src',
  base: './',
  plugins: [vue()],
  build: {
    outDir: '..',
    emptyOutDir: false,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name].[ext]',
      },
    },
  },
})
