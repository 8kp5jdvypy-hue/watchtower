import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      // Two static pages, one deploy (docs/phase4-proof-engine-proposal.md,
      // Part A's route decision) -- /record is a second Vite entry point,
      // not a client-side route.
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        record: fileURLToPath(new URL('./record.html', import.meta.url)),
      },
      output: {
        // The hero's WebGL scene needs three/@react-three/*/postprocessing
        // and gsap immediately -- none of that can be deferred without
        // touching the opening animation, which is explicitly off-limits.
        // Splitting them into their own chunk doesn't change *when*
        // anything loads (everything here is still imported eagerly, so
        // total time-to-interactive for this deploy is unchanged) -- it
        // changes what happens on the *next* deploy: a change to page
        // copy or a marketing component gets a new hash only for the
        // small app chunk, and a repeat visitor's already-cached vendor
        // chunk (see public/_headers' immutable Cache-Control) is
        // reused untouched instead of being invalidated by an unrelated
        // change.
        // Rolldown (Vite 8's bundler) only accepts the function form of
        // manualChunks, not Rollup's object shorthand.
        //
        // React/react-dom get their OWN chunk, split out from the
        // three/gsap group below -- discovered building record.html
        // (Part A): without this, react-three-fiber's own dependency on
        // react pulled react itself into the same physical 'vendor'
        // chunk as three/gsap/postprocessing, so /record's tiny bundle
        // was forced to modulepreload the whole ~1MB WebGL chunk just to
        // get React, which it actually needs and three/gsap it doesn't.
        // Splitting the module graph never duplicates a module instance
        // (each resolved file still exists exactly once across all
        // chunks) -- this only changes which physical file it lives in.
        manualChunks(id) {
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) {
            return 'react-vendor'
          }
          if (/node_modules\/(three|@react-three|postprocessing|gsap)\//.test(id)) {
            return 'vendor'
          }
        },
      },
    },
  },
})
